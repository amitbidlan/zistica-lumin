"""Tests for the agent-first API surface — Phase 1 of the agent-card UX.

Treats `traces.name` as the agent identity. Aggregates metrics +
exposes a detail view; both endpoints derive entirely from the
existing traces / spans / policy_violations tables.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _post_root(client, *, id_, name, session_id=None, started_at=None,
               ended_at=None):
    return client.post(
        "/v1/spans",
        json={"spans": [{
            "id": id_,
            "trace_id": id_,
            "name": name,
            "started_at": started_at or datetime.now(timezone.utc).isoformat(),
            "ended_at": ended_at,
            "session_id": session_id,
        }]},
    )


def _post_child_llm(client, *, parent_id, span_id, model="gpt-4o-mini",
                    tokens_in=100, tokens_out=50, cost=0.0001, error=None):
    body = {
        "id": span_id,
        "trace_id": parent_id,
        "parent_span_id": parent_id,
        "name": "llm.call",
        "type": "llm",
        "started_at": "2026-05-04T10:00:00.100Z",
        "ended_at": "2026-05-04T10:00:01.100Z",
        "model": model,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "cost_usd": cost,
    }
    if error:
        body["error"] = error
        body["status"] = "error"
    return client.post("/v1/spans", json={"spans": [body]})


# ---------- /v1/agents -----------------------------------------------------


def test_agents_list_returns_distinct_names(client):
    """Distinct trace.name values become agent entries; counts are
    correct."""
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="t1", name="customer_support_bot", started_at=base)
    _post_root(client, id_="t2", name="customer_support_bot", started_at=base)
    _post_root(client, id_="t3", name="research_agent", started_at=base)

    body = client.get("/v1/agents").json()
    by_name = {a["name"]: a for a in body["agents"]}
    assert "customer_support_bot" in by_name
    assert "research_agent" in by_name
    assert by_name["customer_support_bot"]["trace_count"] == 2
    assert by_name["research_agent"]["trace_count"] == 1


def test_agents_aggregate_cost_and_tokens(client):
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="ag-1", name="cost_agent", started_at=base)
    _post_child_llm(client, parent_id="ag-1", span_id="ag-1-llm-a",
                    tokens_in=100, tokens_out=50, cost=0.01)
    _post_child_llm(client, parent_id="ag-1", span_id="ag-1-llm-b",
                    tokens_in=200, tokens_out=100, cost=0.02)

    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "cost_agent")
    assert abs(a["total_cost_usd"] - 0.03) < 0.0001
    assert a["total_tokens"] == 100 + 50 + 200 + 100


def test_agents_search_filters_substring(client):
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="sa-1", name="customer_support_bot", started_at=base)
    _post_root(client, id_="sa-2", name="research_agent", started_at=base)
    _post_root(client, id_="sa-3", name="billing_helper", started_at=base)

    body = client.get("/v1/agents?search=support").json()
    names = {a["name"] for a in body["agents"]}
    assert "customer_support_bot" in names
    assert "research_agent" not in names


def test_agents_window_excludes_old_traces(client):
    """Traces outside the window don't contribute to metrics."""
    very_old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="old-1", name="window_agent", started_at=very_old)
    _post_root(client, id_="new-1", name="window_agent", started_at=new)

    body = client.get("/v1/agents?window_hours=24").json()
    a = next(x for x in body["agents"] if x["name"] == "window_agent")
    # Only the new one falls inside the 24h window
    assert a["trace_count"] == 1


def test_agents_skip_unnamed_traces(client):
    """`name` NULL or empty → not surfaced as an agent."""
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="named-1", name="real_agent", started_at=base)
    # Trace with no name → still ingested but invisible to /v1/agents
    client.post("/v1/spans", json={"spans": [{
        "id": "no-name", "trace_id": "no-name",
        "started_at": base,
    }]})
    body = client.get("/v1/agents").json()
    names = {a["name"] for a in body["agents"]}
    assert "real_agent" in names
    # The empty-name span shouldn't appear under any synthetic key
    assert "" not in names
    assert None not in names


def test_agents_top_model_picked_correctly(client):
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="tm-1", name="model_agent", started_at=base)
    # 3 calls of gpt-4o-mini, 1 call of claude-sonnet-4 — gpt-4o-mini wins
    for i, m in enumerate(["gpt-4o-mini", "gpt-4o-mini", "gpt-4o-mini",
                           "claude-sonnet-4"]):
        _post_child_llm(client, parent_id="tm-1", span_id=f"tm-1-{i}", model=m)
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "model_agent")
    assert a["top_model"] == "gpt-4o-mini"


def test_agents_violations_count_from_policy_table(client, db):
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="vt-1", name="naughty_agent", started_at=base)
    _post_root(client, id_="vt-2", name="naughty_agent", started_at=base)
    # Add 2 violations against 2 traces of the same agent
    db.execute(
        """INSERT INTO policy_violations (id, policy_name, trace_id, severity, action_taken)
           VALUES (?, ?, ?, ?, ?)""",
        ["va-1", "p", "vt-1", "high", "alert"],
    )
    db.execute(
        """INSERT INTO policy_violations (id, policy_name, trace_id, severity, action_taken)
           VALUES (?, ?, ?, ?, ?)""",
        ["va-2", "p", "vt-2", "medium", "flag"],
    )
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "naughty_agent")
    assert a["violation_count"] == 2
    assert a["has_violations"] is True


def test_agents_error_rate_is_fraction_of_traces_with_errors(client):
    """error_rate = traces with at least one error span / total."""
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="e-1", name="err_agent", started_at=base)
    _post_root(client, id_="e-2", name="err_agent", started_at=base)
    _post_root(client, id_="e-3", name="err_agent", started_at=base)
    # 1 of 3 traces has an error span
    _post_child_llm(client, parent_id="e-1", span_id="e-1-bad", error="boom")
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "err_agent")
    assert abs(a["error_rate"] - 1/3) < 0.001


# ---------- activity buckets -----------------------------------------------


def test_agents_activity_label_from_recency(client):
    """Activity bucket reflects how recently the last span landed."""
    now = datetime.now(timezone.utc)
    _post_root(client, id_="a-active", name="recent_agent",
               started_at=now.isoformat())
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "recent_agent")
    # Just-ingested → "active"
    assert a["activity"] in ("active", "idle")  # tiny race window for "idle"


# ---------- /v1/agents/{name} ----------------------------------------------


def test_agent_detail_returns_recent_traces_and_breakdown(client, db):
    base = datetime.now(timezone.utc).isoformat()
    for i in range(3):
        _post_root(client, id_=f"d-{i}", name="detail_agent", started_at=base)
    db.execute(
        """INSERT INTO policy_violations (id, policy_name, trace_id, severity, action_taken)
           VALUES (?, ?, ?, ?, ?)""",
        ["dv-1", "cost_runaway", "d-0", "high", "alert"],
    )
    db.execute(
        """INSERT INTO policy_violations (id, policy_name, trace_id, severity, action_taken)
           VALUES (?, ?, ?, ?, ?)""",
        ["dv-2", "cost_runaway", "d-1", "high", "alert"],
    )
    db.execute(
        """INSERT INTO policy_violations (id, policy_name, trace_id, severity, action_taken)
           VALUES (?, ?, ?, ?, ?)""",
        ["dv-3", "slow_llm", "d-2", "medium", "flag"],
    )
    body = client.get("/v1/agents/detail_agent").json()
    assert body["name"] == "detail_agent"
    assert body["trace_count"] == 3
    assert body["violation_count"] == 3
    assert body["violations_by_policy"] == {"cost_runaway": 2, "slow_llm": 1}
    assert body["violations_by_severity"] == {"high": 2, "medium": 1}
    assert len(body["recent_traces"]) == 3


def test_agent_detail_404_on_unknown_agent(client):
    r = client.get("/v1/agents/no_such_agent")
    assert r.status_code == 404


def test_agent_detail_handles_dot_in_name(client):
    """Trace names like `agent.generate` (used by VoltAgent) must be
    routable. FastAPI's `:path` converter accepts dots."""
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="dot-1", name="agent.generate", started_at=base)
    r = client.get("/v1/agents/agent.generate")
    assert r.status_code == 200
    assert r.json()["name"] == "agent.generate"


# ---------- project (framework) grouping -----------------------------------


def _post_with_project(client, *, id_, name, project=None):
    headers = {"X-Lumin-Project": project} if project else {}
    return client.post(
        "/v1/spans",
        headers=headers,
        json={"spans": [{
            "id": id_,
            "trace_id": id_,
            "name": name,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }]},
    )


def test_agent_card_carries_project_from_header(client):
    """X-Lumin-Project header → traces.project → agent card."""
    _post_with_project(client, id_="p-oc", name="research_bot", project="openclaw")
    _post_with_project(client, id_="p-ma", name="support_bot", project="mastra")
    _post_with_project(client, id_="p-vt", name="planner_bot", project="voltagent")
    body = client.get("/v1/agents").json()
    by_name = {a["name"]: a for a in body["agents"]}
    assert by_name["research_bot"]["project"] == "openclaw"
    assert by_name["support_bot"]["project"] == "mastra"
    assert by_name["planner_bot"]["project"] == "voltagent"


def test_agent_list_response_includes_distinct_projects(client):
    _post_with_project(client, id_="dp-1", name="a", project="openclaw")
    _post_with_project(client, id_="dp-2", name="b", project="mastra")
    _post_with_project(client, id_="dp-3", name="c", project=None)  # → "default"
    body = client.get("/v1/agents").json()
    assert set(body["projects"]) == {"openclaw", "mastra", "default"}


def test_agents_filter_by_project(client):
    _post_with_project(client, id_="fp-1", name="oc_one", project="openclaw")
    _post_with_project(client, id_="fp-2", name="oc_two", project="openclaw")
    _post_with_project(client, id_="fp-3", name="ma_one", project="mastra")
    body = client.get("/v1/agents?project=openclaw").json()
    names = {a["name"] for a in body["agents"]}
    assert names == {"oc_one", "oc_two"}


def test_agents_default_project_when_header_missing(client):
    """No X-Lumin-Project header → project = 'default'."""
    client.post("/v1/spans", json={"spans": [{
        "id": "no-hdr", "trace_id": "no-hdr", "name": "no_header_agent",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }]})
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "no_header_agent")
    assert a["project"] == "default"


# ---- project allowlist (closed-set framework dimension) -----------------


def test_unknown_project_is_normalized_to_default(client):
    """X-Lumin-Project: live_demo (a free-form value) must NOT show up
    as a top-level framework section. The ingest path folds anything
    outside the allowlist into "default" so the headline list stays
    stable."""
    _post_with_project(client, id_="ld-1", name="phase2_live_agent", project="live_demo")
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "phase2_live_agent")
    assert a["project"] == "default"
    assert "live_demo" not in body["projects"]


def test_project_header_is_case_insensitive(client):
    """OpenClaw → openclaw — header case shouldn't fragment grouping."""
    _post_with_project(client, id_="case-1", name="cap_agent", project="OpenClaw")
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "cap_agent")
    assert a["project"] == "openclaw"


def test_project_allowlist_keeps_known_values_intact(client):
    """All four allowlisted values pass through unchanged."""
    cases = [("ok-1", "a", "openclaw"), ("ok-2", "b", "mastra"),
             ("ok-3", "c", "voltagent"), ("ok-4", "d", "default")]
    for id_, name, proj in cases:
        _post_with_project(client, id_=id_, name=name, project=proj)
    body = client.get("/v1/agents").json()
    by_name = {a["name"]: a["project"] for a in body["agents"]}
    assert by_name == {"a": "openclaw", "b": "mastra", "c": "voltagent", "d": "default"}


def test_agents_list_distinct_providers_per_agent(client):
    """Agent uses both anthropic + openai → providers list has both."""
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="mp-1", name="multi_provider_agent", started_at=base)
    _post_child_llm(client, parent_id="mp-1", span_id="mp-1-a", model="claude-sonnet-4")
    # Manually post a span with a different provider
    client.post("/v1/spans", json={"spans": [{
        "id": "mp-1-b", "trace_id": "mp-1", "parent_span_id": "mp-1",
        "name": "llm.call", "type": "llm",
        "started_at": "2026-05-04T10:00:00Z", "ended_at": "2026-05-04T10:00:01Z",
        "model": "claude-sonnet-4", "provider": "anthropic",
        "tokens_input": 10, "tokens_output": 5, "cost_usd": 0.001,
    }]})
    client.post("/v1/spans", json={"spans": [{
        "id": "mp-1-c", "trace_id": "mp-1", "parent_span_id": "mp-1",
        "name": "llm.call", "type": "llm",
        "started_at": "2026-05-04T10:00:00Z", "ended_at": "2026-05-04T10:00:01Z",
        "model": "gpt-4o-mini", "provider": "openai",
        "tokens_input": 10, "tokens_output": 5, "cost_usd": 0.001,
    }]})
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "multi_provider_agent")
    assert "anthropic" in a["providers"]
    assert "openai" in a["providers"]


def test_agents_filter_by_provider(client):
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="fpv-1", name="anth_only", started_at=base)
    client.post("/v1/spans", json={"spans": [{
        "id": "fpv-1-llm", "trace_id": "fpv-1", "parent_span_id": "fpv-1",
        "name": "llm", "type": "llm", "started_at": base, "ended_at": base,
        "model": "claude-sonnet-4", "provider": "anthropic",
        "tokens_input": 1, "tokens_output": 1, "cost_usd": 0.0001,
    }]})
    _post_root(client, id_="fpv-2", name="openai_only", started_at=base)
    client.post("/v1/spans", json={"spans": [{
        "id": "fpv-2-llm", "trace_id": "fpv-2", "parent_span_id": "fpv-2",
        "name": "llm", "type": "llm", "started_at": base, "ended_at": base,
        "model": "gpt-4o", "provider": "openai",
        "tokens_input": 1, "tokens_output": 1, "cost_usd": 0.0001,
    }]})
    body = client.get("/v1/agents?provider=anthropic").json()
    names = {a["name"] for a in body["agents"]}
    assert "anth_only" in names
    assert "openai_only" not in names


def test_agent_card_has_activity_buckets_and_active_traces_fields(client):
    """Phase 2: every card carries 12-bucket sparkline + active_traces."""
    base = datetime.now(timezone.utc).isoformat()
    _post_root(client, id_="ph2-1", name="phase2_agent", started_at=base)
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "phase2_agent")
    # Schema present even when no in-flight traces
    assert isinstance(a["active_traces"], int)
    assert isinstance(a["activity_buckets"], list)
    assert len(a["activity_buckets"]) == 12


def test_active_traces_counts_open_recent_traces(client):
    """Trace started in the last 10 min, no ended_at → active_traces = 1.
    Trace started but already ended → not counted.
    Trace started > 10 min ago without ended_at → orphan, not counted."""
    now = datetime.now(timezone.utc)
    # Open recent trace
    client.post("/v1/spans", json={"spans": [{
        "id": "act-recent", "trace_id": "act-recent",
        "name": "active_check_agent",
        "started_at": now.isoformat(),
        # ended_at omitted → trace has NULL ended_at
    }]})
    # Closed trace
    client.post("/v1/spans", json={"spans": [{
        "id": "act-closed", "trace_id": "act-closed",
        "name": "active_check_agent",
        "started_at": now.isoformat(),
        "ended_at": now.isoformat(),
    }]})
    # Old orphan
    old = (now - timedelta(hours=2)).isoformat()
    client.post("/v1/spans", json={"spans": [{
        "id": "act-old", "trace_id": "act-old",
        "name": "active_check_agent",
        "started_at": old,
        # no ended_at, but old → excluded
    }]})

    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "active_check_agent")
    assert a["active_traces"] == 1


def test_activity_buckets_count_traces_per_5min(client):
    """Bucket 0 = most recent. A trace started right now lands in
    bucket 0 (or near it)."""
    now = datetime.now(timezone.utc)
    for i in range(3):
        client.post("/v1/spans", json={"spans": [{
            "id": f"buck-{i}", "trace_id": f"buck-{i}",
            "name": "spark_agent",
            "started_at": now.isoformat(),
            "ended_at": now.isoformat(),
        }]})
    body = client.get("/v1/agents").json()
    a = next(x for x in body["agents"] if x["name"] == "spark_agent")
    # All 3 traces should be in the most-recent bucket(s) (0 or 1)
    total = sum(a["activity_buckets"])
    assert total == 3
    # And concentrated in the early buckets (< 10 min ago)
    assert sum(a["activity_buckets"][:2]) >= 3


def test_agent_detail_carries_project_and_providers(client):
    base = datetime.now(timezone.utc).isoformat()
    headers = {"X-Lumin-Project": "openclaw"}
    client.post("/v1/spans", headers=headers, json={"spans": [{
        "id": "ad-1", "trace_id": "ad-1", "name": "detail_agent_oc",
        "started_at": base,
    }]})
    client.post("/v1/spans", headers=headers, json={"spans": [{
        "id": "ad-1-llm", "trace_id": "ad-1", "parent_span_id": "ad-1",
        "name": "llm", "type": "llm", "started_at": base, "ended_at": base,
        "model": "claude-sonnet-4", "provider": "anthropic",
        "tokens_input": 10, "tokens_output": 5, "cost_usd": 0.001,
    }]})
    detail = client.get("/v1/agents/detail_agent_oc").json()
    assert detail["project"] == "openclaw"
    assert "anthropic" in detail["providers"]


# ---- older_data_exists hint ----------------------------------------------


def test_older_data_exists_false_when_db_empty(client):
    """Empty DB → no older data, no hint to show."""
    body = client.get("/v1/agents").json()
    assert body["older_data_exists"] is False


def test_older_data_exists_false_when_recent_only(client):
    """Recent traces only → window covers them all → no hint."""
    _post_with_project(client, id_="recent", name="recent_agent")
    body = client.get("/v1/agents").json()
    assert body["older_data_exists"] is False
    assert len(body["agents"]) == 1


def test_older_data_exists_true_when_data_outside_window(client):
    """Trace before window cutoff → hint should show. This is the
    scenario the user hit: /traces shows rows but /agents (24h window)
    is empty — operator needed a nudge to widen the filter.
    """
    # Trace from 5 days ago — outside 24h window
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    client.post("/v1/spans", json={"spans": [{
        "id": "old-trace", "trace_id": "old-trace",
        "name": "ancient_agent", "started_at": old,
    }]})
    body = client.get("/v1/agents").json()
    assert body["older_data_exists"] is True
    assert len(body["agents"]) == 0  # outside 24h window

    # Widen to 7d → agent shows up, hint clears
    body7 = client.get("/v1/agents?window_hours=168").json()
    assert body7["older_data_exists"] is False
    assert any(a["name"] == "ancient_agent" for a in body7["agents"])


def test_older_data_ignores_unnamed_traces(client):
    """Stub traces (no root span yet, name=NULL) shouldn't trigger
    the hint — they're not user-visible agents and would just
    confuse the empty state.
    """
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    # Child span without a root → trace stub created with name=NULL
    client.post("/v1/spans", json={"spans": [{
        "id": "child-only", "trace_id": "stub-trace",
        "parent_span_id": "never-arrives",
        "started_at": old,
    }]})
    body = client.get("/v1/agents").json()
    assert body["older_data_exists"] is False
