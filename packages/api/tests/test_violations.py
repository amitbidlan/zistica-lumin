"""Tests for the Policy Engine API endpoints — Accountability Layer Part B."""


def _ingest(client, **fields):
    body = {
        "violations": [{
            "policy_name": fields.get("policy_name", "p1"),
            "severity": fields.get("severity", "high"),
            "trace_id": fields.get("trace_id", "trace-1"),
            "span_id": fields.get("span_id"),
            "condition_text": fields.get("condition_text", "trace.total_cost_usd > 0.10"),
            "action_taken": fields.get("action_taken", "flag"),
            "policy_description": fields.get("policy_description"),
            "actual_value": fields.get("actual_value"),
            "webhook_fired": fields.get("webhook_fired", False),
            "webhook_url": fields.get("webhook_url"),
        }]
    }
    return client.post("/v1/violations", json=body)


def _post_trace_with_root_span(client, trace_id="trace-1", **kw):
    """Helper: post a root span so a trace row exists for trace_id."""
    return client.post(
        "/v1/spans",
        json={
            "spans": [{
                "id": trace_id,
                "trace_id": trace_id,
                "name": kw.get("name", "agent.run"),
                "started_at": "2026-05-04T10:00:00Z",
                "ended_at": "2026-05-04T10:00:01Z",
            }]
        },
    )


def test_violation_stored_in_db(client):
    """Policy triggered → violation in GET /v1/violations."""
    resp = _ingest(client, policy_name="max_cost_per_trace",
                   severity="high", trace_id="trace-x")
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}

    listing = client.get("/v1/violations").json()
    assert listing["total"] == 1
    assert len(listing["violations"]) == 1
    v = listing["violations"][0]
    assert v["policy_name"] == "max_cost_per_trace"
    assert v["severity"] == "high"
    assert v["trace_id"] == "trace-x"


def test_filter_by_severity(client):
    _ingest(client, severity="high", trace_id="t-h")
    _ingest(client, severity="low", trace_id="t-l")
    high = client.get("/v1/violations?severity=high").json()
    low = client.get("/v1/violations?severity=low").json()
    assert high["total"] == 1
    assert low["total"] == 1
    assert high["violations"][0]["severity"] == "high"
    assert low["violations"][0]["severity"] == "low"


def test_filter_by_trace_id(client):
    _ingest(client, trace_id="t-1")
    _ingest(client, trace_id="t-2")
    only_t1 = client.get("/v1/violations?trace_id=t-1").json()
    assert only_t1["total"] == 1
    assert only_t1["violations"][0]["trace_id"] == "t-1"


def test_filter_by_policy_name(client):
    _ingest(client, policy_name="cost_check")
    _ingest(client, policy_name="latency_check")
    cost = client.get("/v1/violations?policy_name=cost_check").json()
    assert cost["total"] == 1
    assert cost["violations"][0]["policy_name"] == "cost_check"


def test_pagination(client):
    for i in range(5):
        _ingest(client, policy_name=f"p_{i}", trace_id=f"t_{i}")
    page1 = client.get("/v1/violations?limit=2").json()
    page2 = client.get("/v1/violations?limit=2&offset=2").json()
    assert len(page1["violations"]) == 2
    assert len(page2["violations"]) == 2
    assert page1["total"] == 5
    assert page2["total"] == 5
    # Disjoint pages
    ids1 = {v["id"] for v in page1["violations"]}
    ids2 = {v["id"] for v in page2["violations"]}
    assert ids1.isdisjoint(ids2)


def test_stats_counts(client):
    _ingest(client, severity="high", policy_name="cost", trace_id="t1")
    _ingest(client, severity="high", policy_name="cost", trace_id="t2")
    _ingest(client, severity="low", policy_name="latency", trace_id="t3")
    stats = client.get("/v1/violations/stats").json()
    assert stats["total"] == 3
    assert stats["by_severity"] == {"high": 2, "low": 1}
    assert stats["by_policy"] == {"cost": 2, "latency": 1}


def test_stats_empty(client):
    stats = client.get("/v1/violations/stats").json()
    assert stats["total"] == 0
    assert stats["by_severity"] == {}
    assert stats["by_policy"] == {}


def test_trace_response_includes_violations(client):
    """GET /v1/traces/{id} → has_violations: true and a list of summaries."""
    _post_trace_with_root_span(client, trace_id="trace-with-v")
    _ingest(client, policy_name="max_cost", severity="high",
            trace_id="trace-with-v")
    _ingest(client, policy_name="slow_span", severity="medium",
            trace_id="trace-with-v")
    body = client.get("/v1/traces/trace-with-v").json()
    assert body["has_violations"] is True
    names = {v["policy_name"] for v in body["policy_violations"]}
    assert names == {"max_cost", "slow_span"}


def test_trace_response_no_violations_is_clean(client):
    """A trace with no violations → has_violations: false, empty list."""
    _post_trace_with_root_span(client, trace_id="trace-clean")
    body = client.get("/v1/traces/trace-clean").json()
    assert body["has_violations"] is False
    assert body["policy_violations"] == []


def test_violation_actual_value_round_trips(client):
    _ingest(client, actual_value="0.50", trace_id="t-rt")
    listing = client.get("/v1/violations?trace_id=t-rt").json()
    assert listing["violations"][0]["actual_value"] == "0.50"


def test_webhook_fired_field_round_trips(client):
    _ingest(client, webhook_fired=True, webhook_url="https://x.example",
            trace_id="t-wh")
    listing = client.get("/v1/violations?trace_id=t-wh").json()
    v = listing["violations"][0]
    assert v["webhook_fired"] is True
    assert v["webhook_url"] == "https://x.example"
