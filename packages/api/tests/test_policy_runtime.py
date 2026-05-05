"""Tests for server-side Policy Engine — every span POSTed to /v1/spans
gets evaluated against the loaded policy file. Critical for the TS
integrations (Mastra/OpenClaw/VoltAgent) that can't use the Python
SDK's in-process dispatcher.
"""

from pathlib import Path

import pytest


@pytest.fixture
def policy_file(tmp_path: Path):
    """Load a focused policy file via LUMIN_POLICY_FILE for the test
    process, and reset the runtime's cached engine so each test gets
    a fresh load."""
    f = tmp_path / "policies.yaml"
    f.write_text("""
version: 1
policies:
  - name: slow_llm_call
    trigger: span_end
    condition: "span.type == 'llm' and span.duration_ms > 100"
    action: alert
    severity: medium
  - name: tool_runaway_loop
    trigger: trace_end
    condition: "trace.span_count > 5"
    action: alert
    severity: high
  - name: any_error
    trigger: trace_end
    condition: "trace.error_count > 0"
    action: flag
    severity: low
""", encoding="utf-8")
    import os
    import policy_runtime as pr
    old = os.environ.get("LUMIN_POLICY_FILE")
    os.environ["LUMIN_POLICY_FILE"] = str(f)
    pr._engine = None
    pr._engine_loaded = False
    yield f
    pr._engine = None
    pr._engine_loaded = False
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


def test_span_end_policy_fires_at_ingest(client, policy_file):
    """A slow LLM span POSTed → policy fires server-side, no SDK needed."""
    _post_span(
        client,
        id="slow-llm-1",
        trace_id="t1",
        type="llm",
        model="gpt-4o",
        started_at="2026-05-04T10:00:00.000Z",
        ended_at="2026-05-04T10:00:00.500Z",  # 500 ms — over the 100 ms threshold
    )
    listing = client.get("/v1/violations?trace_id=t1").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "slow_llm_call" in names


def test_span_end_policy_does_not_fire_when_clean(client, policy_file):
    _post_span(
        client,
        id="quick-llm",
        trace_id="t-quick",
        type="llm",
        model="gpt-4o",
        started_at="2026-05-04T10:00:00.000Z",
        ended_at="2026-05-04T10:00:00.020Z",  # 20 ms — under 100 ms threshold
    )
    listing = client.get("/v1/violations?trace_id=t-quick").json()
    assert listing["violations"] == []


def test_trace_end_runaway_loop_fires(client, policy_file):
    """When >5 spans land + a root, tool_runaway_loop should fire."""
    trace_id = "t-runaway"
    # Post 6 child spans + 1 root = 7 total
    for i in range(6):
        _post_span(
            client,
            id=f"child-{i}",
            trace_id=trace_id,
            parent_span_id="root-r",
            name=f"step_{i}",
            type="custom",
        )
    # Root last — triggers trace_end eval
    _post_span(
        client,
        id="root-r",
        trace_id=trace_id,
        name="agent.run",
    )
    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "tool_runaway_loop" in names


def test_trace_end_no_loop_does_not_fire(client, policy_file):
    trace_id = "t-small"
    for i in range(2):
        _post_span(client, id=f"c-{i}", trace_id=trace_id, parent_span_id="root-s")
    _post_span(client, id="root-s", trace_id=trace_id, name="small_run")
    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "tool_runaway_loop" not in names


def test_trace_end_any_error_fires_when_child_has_error(client, policy_file):
    trace_id = "t-err"
    _post_span(
        client,
        id="bad-child",
        trace_id=trace_id,
        parent_span_id="root-e",
        type="tool",
        status="error",
        error="upstream HTTP 503",
    )
    _post_span(client, id="root-e", trace_id=trace_id, name="agent_with_error")
    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "any_error" in names


def test_no_policy_file_means_no_eval(client, tmp_path):
    """When LUMIN_POLICY_FILE isn't set, server-side eval is a no-op
    and no violations land — even when conditions would be met."""
    import os
    import policy_runtime as pr
    old = os.environ.pop("LUMIN_POLICY_FILE", None)
    pr._engine = None
    pr._engine_loaded = False
    try:
        _post_span(
            client,
            id="quiet-span",
            trace_id="t-quiet",
            type="llm",
            started_at="2026-05-04T10:00:00.000Z",
            ended_at="2026-05-04T10:00:01.000Z",  # 1000 ms
        )
        listing = client.get("/v1/violations?trace_id=t-quiet").json()
        assert listing["violations"] == []
    finally:
        pr._engine = None
        pr._engine_loaded = False
        if old is not None:
            os.environ["LUMIN_POLICY_FILE"] = old


def test_invalid_policy_file_disables_engine_cleanly(client, tmp_path):
    """Bad YAML at startup → engine None → ingest still works, no
    violations recorded, no exception propagated."""
    import os
    import policy_runtime as pr
    bad = tmp_path / "bad.yaml"
    bad.write_text("policies:\n  - name: missing_required_fields", encoding="utf-8")
    old = os.environ.get("LUMIN_POLICY_FILE")
    os.environ["LUMIN_POLICY_FILE"] = str(bad)
    pr._engine = None
    pr._engine_loaded = False
    try:
        # Ingest still succeeds
        resp = _post_span(client, id="x1", trace_id="x1", name="a")
        assert resp.status_code == 200
        # No violations because the engine is None
        listing = client.get("/v1/violations?trace_id=x1").json()
        assert listing["violations"] == []
    finally:
        pr._engine = None
        pr._engine_loaded = False
        if old is None:
            os.environ.pop("LUMIN_POLICY_FILE", None)
        else:
            os.environ["LUMIN_POLICY_FILE"] = old


def test_evaluation_aggregates_from_spans_not_stored_columns(client, policy_file):
    """Real-world Mastra/VoltAgent pattern: spans land via /v1/spans
    only, traces.total_cost_usd column stays 0. trace_end aggregation
    must read from spans, mirroring the sessions-endpoint fix."""
    trace_id = "t-agg"
    # 6 child spans + root = 7 → triggers tool_runaway_loop, AND we
    # verify the engine sees the right span_count in the trace dict.
    for i in range(6):
        _post_span(
            client,
            id=f"agg-{i}",
            trace_id=trace_id,
            parent_span_id="root-agg",
            type="llm",
            model="gpt-4o-mini",
            tokens_input=100,
            tokens_output=50,
            cost_usd=0.0001,
        )
    _post_span(client, id="root-agg", trace_id=trace_id, name="aggregating_run")
    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    runaway = [v for v in listing["violations"] if v["policy_name"] == "tool_runaway_loop"]
    assert len(runaway) == 1
    # actual_value captures the threshold-CROSSING moment (the 6th span
    # is when span_count went from 5 to 6, crossing > 5). Production
    # fix: re-eval runs on every span; the deterministic id + ON CONFLICT
    # means the FIRST violation row wins. That's strictly more useful
    # than the previous "at root-end" snapshot — operators see when the
    # policy first triggered, not the final state.
    assert runaway[0]["actual_value"] == "6"
