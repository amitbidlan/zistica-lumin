"""Tests for Phase 3 (per-agent scope) and Phase 4 (DB-backed CRUD).

Phase 3 tests use a YAML policy file so we can exercise the
``scope.agents`` parsing + engine filter without touching the new DB
tables.

Phase 4 tests use the CRUD endpoints directly. ``client`` is the
TestClient with the in-memory DB injected via dependency override —
so a POST /v1/policies writes to the test DB, not the prod one. The
CRUD handler calls ``policy_runtime.reload_engine(db=db)`` after
each write, which means the engine ends up sourced from the test DB
and is in scope for the same client.
"""

from pathlib import Path

import pytest


# ---- Phase 3 — scope -----------------------------------------------------


@pytest.fixture
def scoped_policy_file(tmp_path: Path):
    """Two policies — one un-scoped (applies to all agents), one scoped
    to a single agent name. Lets us assert both branches of the
    Policy.applies_to_agent() filter in one fixture."""
    f = tmp_path / "scoped_policies.yaml"
    f.write_text("""
version: 1
policies:
  - name: long_run_universal
    description: Trace span_count over 5 — applies to every agent
    trigger: trace_end
    condition: "trace.span_count > 5"
    action: alert
    severity: medium

  - name: long_run_only_for_billing
    description: Same threshold but only fires for billing_agent
    trigger: trace_end
    condition: "trace.span_count > 5"
    action: alert
    severity: high
    scope:
      agents:
        - billing_agent
""", encoding="utf-8")
    import os
    import policy_runtime as pr
    old = os.environ.get("LUMIN_POLICY_FILE")
    os.environ["LUMIN_POLICY_FILE"] = str(f)
    pr._reset_for_tests()
    yield f
    pr._reset_for_tests()
    if old is None:
        os.environ.pop("LUMIN_POLICY_FILE", None)
    else:
        os.environ["LUMIN_POLICY_FILE"] = old


def _post_span(client, **kw):
    body = {"spans": [{
        "id": kw["id"],
        "trace_id": kw.get("trace_id", kw["id"]),
        "parent_span_id": kw.get("parent_span_id"),
        "name": kw.get("name", "x"),
        "type": kw.get("type", "custom"),
        "started_at": kw.get("started_at", "2026-05-04T10:00:00Z"),
        "ended_at": kw.get("ended_at", "2026-05-04T10:00:00.050Z"),
        "model": kw.get("model"),
        "tokens_input": kw.get("tokens_input"),
        "tokens_output": kw.get("tokens_output"),
        "cost_usd": kw.get("cost_usd"),
        "error": kw.get("error"),
        "status": kw.get("status"),
    }]}
    return client.post("/v1/spans", json=body)


def _drive_runaway_trace(client, trace_id: str, agent_name: str) -> None:
    """Push 6 child spans + a root with the given agent name."""
    for i in range(6):
        _post_span(
            client,
            id=f"{trace_id}-c-{i}",
            trace_id=trace_id,
            parent_span_id=f"{trace_id}-root",
            type="custom",
        )
    _post_span(client, id=f"{trace_id}-root", trace_id=trace_id, name=agent_name)


def test_scoped_policy_fires_only_for_matching_agent(client, scoped_policy_file):
    _drive_runaway_trace(client, "t-billing", agent_name="billing_agent")

    listing = client.get("/v1/violations?trace_id=t-billing").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "long_run_universal" in names
    assert "long_run_only_for_billing" in names


def test_scoped_policy_skipped_for_other_agent(client, scoped_policy_file):
    _drive_runaway_trace(client, "t-other", agent_name="customer_support_agent")

    listing = client.get("/v1/violations?trace_id=t-other").json()
    names = {v["policy_name"] for v in listing["violations"]}
    # Universal still fires; scoped one does NOT
    assert "long_run_universal" in names
    assert "long_run_only_for_billing" not in names


def test_scoped_policy_skipped_when_agent_name_missing(client, scoped_policy_file):
    """Trace without a name (orphan child spans only) → no agent
    identity → scoped rules MUST NOT fire (better miss than mis-fire).
    The un-scoped rule still fires because its trigger is satisfied.
    """
    trace_id = "t-anon"
    for i in range(6):
        _post_span(
            client,
            id=f"anon-c-{i}",
            trace_id=trace_id,
            parent_span_id=f"{trace_id}-root",
            type="custom",
        )
    # NB: NO root span is sent — trace.name stays NULL.
    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "long_run_universal" in names
    assert "long_run_only_for_billing" not in names


def test_get_policies_filters_by_agent(client, scoped_policy_file):
    """GET /v1/policies?agent=foo returns only policies that apply to foo."""
    # Trigger an ingest so the engine is eagerly loaded
    _post_span(client, id="warmup", trace_id="warmup", name="any")

    all_resp = client.get("/v1/policies").json()
    assert all_resp["engine_loaded"] is True
    all_names = {p["name"] for p in all_resp["policies"]}
    assert {"long_run_universal", "long_run_only_for_billing"} <= all_names

    only_billing = client.get("/v1/policies?agent=billing_agent").json()
    names_billing = {p["name"] for p in only_billing["policies"]}
    assert "long_run_only_for_billing" in names_billing
    assert "long_run_universal" in names_billing  # un-scoped → applies to all

    only_other = client.get("/v1/policies?agent=customer_support_agent").json()
    names_other = {p["name"] for p in only_other["policies"]}
    assert "long_run_only_for_billing" not in names_other
    assert "long_run_universal" in names_other


def test_invalid_scope_yaml_disables_engine_cleanly(client, tmp_path):
    """Bad scope.agents entry → PolicyConfigError → engine disabled,
    ingest still works."""
    import os
    import policy_runtime as pr
    bad = tmp_path / "bad_scope.yaml"
    bad.write_text("""
version: 1
policies:
  - name: oops
    trigger: trace_end
    condition: "trace.span_count > 1"
    action: flag
    severity: low
    scope:
      agents:
        - 12345
""", encoding="utf-8")
    old = os.environ.get("LUMIN_POLICY_FILE")
    os.environ["LUMIN_POLICY_FILE"] = str(bad)
    pr._reset_for_tests()
    try:
        resp = _post_span(client, id="x1", trace_id="x1", name="agent")
        assert resp.status_code == 200
        # Engine never loaded → no violations
        listing = client.get("/v1/violations?trace_id=x1").json()
        assert listing["violations"] == []
    finally:
        pr._reset_for_tests()
        if old is None:
            os.environ.pop("LUMIN_POLICY_FILE", None)
        else:
            os.environ["LUMIN_POLICY_FILE"] = old


# ---- Phase 4 — CRUD ------------------------------------------------------


@pytest.fixture
def db_engine(client, db):
    """Force the engine to be DB-sourced for this test by clearing
    LUMIN_POLICY_FILE and pre-creating a policy via the CRUD path.

    This isolates Phase 4 tests from the YAML scenarios above —
    once a policy lands in the DB, ``reload_engine`` flips the source
    to "db" and stays there until the table is empty again."""
    import os
    import policy_runtime as pr
    old = os.environ.pop("LUMIN_POLICY_FILE", None)
    pr._reset_for_tests()
    yield db
    pr._reset_for_tests()
    if old is not None:
        os.environ["LUMIN_POLICY_FILE"] = old


def _create_policy(client, **kw):
    body = {
        "name": kw["name"],
        "description": kw.get("description"),
        "trigger": kw.get("trigger", "span_end"),
        "condition": kw.get("condition", "span.duration_ms > 100"),
        "action": kw.get("action", "flag"),
        "severity": kw.get("severity", "low"),
        "webhook_url": kw.get("webhook_url"),
        "scope_agents": kw.get("scope_agents", []),
        "enabled": kw.get("enabled", True),
    }
    return client.post("/v1/policies", json=body)


def test_create_policy_persists_to_db(client, db_engine):
    res = _create_policy(client, name="phase4_first", description="hello")
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "phase4_first"
    assert body["source"] == "db"
    assert body["version"] == 1

    # Round-trip via GET /v1/policies
    listing = client.get("/v1/policies").json()
    assert listing["source"] == "db"
    names = {p["name"] for p in listing["policies"]}
    assert "phase4_first" in names


def test_create_rejects_duplicate_name(client, db_engine):
    _create_policy(client, name="dup")
    res = _create_policy(client, name="dup")
    assert res.status_code == 409


def test_create_rejects_invalid_trigger(client, db_engine):
    res = _create_policy(client, name="bogus", trigger="onmonday")
    assert res.status_code == 400


def test_create_rejects_unparseable_condition(client, db_engine):
    res = _create_policy(client, name="garbage", condition="this is not python !!!")
    assert res.status_code == 400


def test_update_policy_bumps_version(client, db_engine):
    _create_policy(client, name="vbump", severity="low")
    res = client.put("/v1/policies/vbump", json={"severity": "high"})
    assert res.status_code == 200
    body = res.json()
    assert body["severity"] == "high"
    assert body["version"] == 2  # 1 → 2


def test_update_unknown_returns_404(client, db_engine):
    res = client.put("/v1/policies/nope", json={"severity": "low"})
    assert res.status_code == 404


def test_delete_policy_soft_deletes(client, db_engine):
    _create_policy(client, name="goner")
    res = client.delete("/v1/policies/goner")
    assert res.status_code == 204

    # Gone from list
    listing = client.get("/v1/policies").json()
    names = {p["name"] for p in listing["policies"]}
    assert "goner" not in names

    # Re-create with same name → revives
    res2 = _create_policy(client, name="goner", severity="medium")
    assert res2.status_code == 201
    assert res2.json()["severity"] == "medium"


def test_audit_log_records_create_and_update(client, db_engine):
    _create_policy(client, name="audited", severity="low")
    client.put("/v1/policies/audited", json={"severity": "high"})

    audit = client.get("/v1/policies/audited/audit").json()
    assert audit["total"] >= 2
    actions = [e["action"] for e in audit["entries"]]
    # Most recent first → update before create
    assert actions[0] == "update"
    assert actions[-1] == "create"


def test_db_engine_evaluates_span(client, db_engine):
    """End-to-end: create a span_end policy via API → ingest a matching
    span → violation lands. Verifies the engine is actually reading
    from the DB after a CRUD write, not still the YAML-or-nothing fallback.
    """
    _create_policy(
        client,
        name="db_engine_smoke",
        trigger="span_end",
        condition="span.type == 'llm' and span.duration_ms > 100",
        action="alert",
        severity="medium",
    )
    _post_span(
        client,
        id="db-smoke-1",
        trace_id="t-db-smoke",
        type="llm",
        name="db_smoke_agent",
        started_at="2026-05-04T10:00:00.000Z",
        ended_at="2026-05-04T10:00:00.500Z",
    )
    listing = client.get("/v1/violations?trace_id=t-db-smoke").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "db_engine_smoke" in names


def test_db_engine_respects_scope_agents(client, db_engine):
    """Scope.agents on a DB-backed policy filters identically to YAML."""
    _create_policy(
        client,
        name="db_scoped",
        trigger="trace_end",
        condition="trace.span_count > 2",
        action="flag",
        severity="medium",
        scope_agents=["only_this_agent"],
    )
    # Run a trace with the matching agent
    for i in range(3):
        _post_span(client, id=f"sm-{i}", trace_id="t-sm", parent_span_id="root-sm")
    _post_span(client, id="root-sm", trace_id="t-sm", name="only_this_agent")

    listing = client.get("/v1/violations?trace_id=t-sm").json()
    assert "db_scoped" in {v["policy_name"] for v in listing["violations"]}

    # Different agent — should not fire
    for i in range(3):
        _post_span(client, id=f"oth-{i}", trace_id="t-oth", parent_span_id="root-oth")
    _post_span(client, id="root-oth", trace_id="t-oth", name="some_other_agent")

    listing2 = client.get("/v1/violations?trace_id=t-oth").json()
    assert "db_scoped" not in {v["policy_name"] for v in listing2["violations"]}


def test_bootstrap_imports_yaml_when_db_empty(tmp_path, db):
    """bootstrap_from_yaml_if_empty: with an empty policies table and a
    valid YAML file, all rules import. With anything in the table
    (even disabled), bootstrap is a no-op."""
    import policy_store
    yaml_path = tmp_path / "boot.yaml"
    yaml_path.write_text("""
version: 1
policies:
  - name: bootone
    trigger: span_end
    condition: "span.duration_ms > 1"
    action: flag
    severity: low
""", encoding="utf-8")

    n = policy_store.bootstrap_from_yaml_if_empty(db, str(yaml_path))
    assert n == 1
    assert policy_store.has_any_policies(db)

    # Second call — DB has rows now, must be a no-op
    n2 = policy_store.bootstrap_from_yaml_if_empty(db, str(yaml_path))
    assert n2 == 0


def test_bootstrap_skips_when_no_yaml(db):
    import policy_store
    assert policy_store.bootstrap_from_yaml_if_empty(db, None) == 0
    assert policy_store.bootstrap_from_yaml_if_empty(db, "") == 0


def test_state_token_changes_on_write(client, db_engine):
    """policies_state_token is the watcher's reload trigger — must
    change after a CRUD write or the cache will go stale."""
    import policy_store
    before = policy_store.policies_state_token(db_engine)

    _create_policy(client, name="tok1")
    after_create = policy_store.policies_state_token(db_engine)
    assert after_create != before

    client.put("/v1/policies/tok1", json={"severity": "high"})
    after_update = policy_store.policies_state_token(db_engine)
    assert after_update != after_create


# ---- Brutal-test regressions: condition validator hardening ---------------


def test_condition_rejects_dunder_import(client, db_engine):
    """__import__ is syntactically a Name lookup — simpleeval would
    reject it at eval time but parse() accepts it. The brutal-test
    suite (case B7) flagged this as bad UX. We now AST-walk the
    condition and reject unknown names at write time."""
    res = _create_policy(
        client, name="hack_import",
        condition="__import__('os').system('echo HACKED')",
    )
    assert res.status_code == 400
    assert "__import__" in res.json()["detail"] or "identifier" in res.json()["detail"].lower()


def test_condition_rejects_open_call(client, db_engine):
    res = _create_policy(
        client, name="hack_open",
        condition="open('/etc/passwd').read()",
    )
    assert res.status_code == 400


def test_condition_rejects_method_call(client, db_engine):
    """Method calls aren't allowed — engine doesn't expose any methods
    on span/trace, so x.startswith() etc. would only ever fail at eval
    time. Reject at write time so operators don't ship broken rules."""
    res = _create_policy(
        client, name="hack_method",
        condition="span.name.startswith('admin')",
    )
    assert res.status_code == 400


def test_condition_rejects_unknown_identifier(client, db_engine):
    """Reference to a name outside {span, trace} must reject."""
    res = _create_policy(
        client, name="typo_ref",
        condition="trcae.span_count > 5",  # typo'd 'trcae'
    )
    assert res.status_code == 400


def test_condition_allows_safe_functions(client, db_engine):
    """The whitelist (len, str, int, float, abs) must still work."""
    res = _create_policy(
        client, name="safe_funcs_ok",
        trigger="span_end",
        condition="len(str(span.input)) > 100",
    )
    assert res.status_code == 201


def test_condition_allows_complex_boolean_expression(client, db_engine):
    """Multi-clause conditions over allowed names must parse."""
    res = _create_policy(
        client, name="complex_ok",
        trigger="span_end",
        condition="span.type == 'llm' and span.duration_ms > 1000 and 'gpt' in str(span.model)",
    )
    assert res.status_code == 201


def test_condition_rejects_disallowed_function(client, db_engine):
    """A function call to something not in the whitelist must reject."""
    res = _create_policy(
        client, name="bad_func",
        condition="sorted(span.input) == [1, 2]",
    )
    assert res.status_code == 400
    assert "sorted" in res.json()["detail"]
