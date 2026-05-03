def _post_span(client, **overrides):
    span = {
        "id": "span-default",
        "trace_id": "trace-default",
        "name": "agent",
        "type": "custom",
        "started_at": "2026-05-02T10:00:00Z",
        "ended_at": "2026-05-02T10:00:01Z",
    }
    span.update(overrides)
    return client.post("/v1/spans", json={"spans": [span]})


def test_get_traces_returns_list(client):
    _post_span(client, id="r1", trace_id="r1", name="alpha")
    _post_span(client, id="r2", trace_id="r2", name="beta")

    response = client.get("/v1/traces")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2
    names = {t["name"] for t in body}
    assert names == {"alpha", "beta"}


def test_get_traces_pagination(client):
    for i in range(5):
        _post_span(
            client,
            id=f"r{i}",
            trace_id=f"r{i}",
            name=f"agent-{i}",
            started_at=f"2026-05-02T10:00:0{i}Z",
        )
    response = client.get("/v1/traces?limit=2")
    assert len(response.json()) == 2

    response = client.get("/v1/traces?limit=2&offset=2")
    assert len(response.json()) == 2

    response = client.get("/v1/traces?limit=10&offset=4")
    assert len(response.json()) == 1


def test_get_trace_by_id_returns_404_for_missing(client):
    response = client.get("/v1/traces/does-not-exist")
    assert response.status_code == 404


def test_get_trace_returns_duration_ms(client):
    _post_span(
        client,
        id="d1",
        trace_id="d1",
        started_at="2026-05-02T10:00:00Z",
        ended_at="2026-05-02T10:00:01.500Z",
    )
    response = client.get("/v1/traces/d1")
    assert response.status_code == 200
    body = response.json()
    assert body["duration_ms"] == 1500


def test_get_trace_spans_returns_all_spans_for_trace(client):
    _post_span(client, id="root-x", trace_id="x", name="root")
    _post_span(
        client,
        id="child-1",
        trace_id="x",
        parent_span_id="root-x",
        name="child1",
        started_at="2026-05-02T10:00:00.500Z",
    )
    _post_span(
        client,
        id="child-2",
        trace_id="x",
        parent_span_id="root-x",
        name="child2",
        started_at="2026-05-02T10:00:00.700Z",
    )
    # Span on a different trace — must not appear
    _post_span(client, id="other", trace_id="other", name="unrelated")

    response = client.get("/v1/traces/x/spans")
    assert response.status_code == 200
    spans = response.json()
    assert len(spans) == 3
    names = {s["name"] for s in spans}
    assert names == {"root", "child1", "child2"}


def test_parent_child_relationship_preserved(client):
    _post_span(client, id="r", trace_id="r", name="root")
    _post_span(
        client,
        id="c",
        trace_id="r",
        parent_span_id="r",
        name="child",
        started_at="2026-05-02T10:00:00.500Z",
    )
    response = client.get("/v1/traces/r/spans")
    spans = {s["id"]: s for s in response.json()}
    assert spans["r"]["parent_span_id"] is None
    assert spans["c"]["parent_span_id"] == "r"


def test_post_traces_creates_trace_directly(client):
    response = client.post(
        "/v1/traces",
        json={
            "id": "manual-1",
            "name": "manual",
            "started_at": "2026-05-02T10:00:00Z",
            "ended_at": "2026-05-02T10:00:02Z",
            "total_tokens": 123,
            "total_cost_usd": 0.0042,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "manual-1"
    assert body["total_tokens"] == 123

    # And it's queryable
    response = client.get("/v1/traces/manual-1")
    assert response.status_code == 200
    assert response.json()["name"] == "manual"


def test_trace_total_cost_is_aggregated_from_child_spans(client):
    """When only spans are POSTed (no explicit /v1/traces upsert),
    GET /v1/traces/{id} should still report total_cost_usd as the
    sum of child span costs — previously it returned 0."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "p", "trace_id": "agg-1", "name": "claude_call",
                    "type": "llm",
                    "started_at": "2026-05-02T10:00:00Z",
                    "ended_at": "2026-05-02T10:00:03Z",
                    "tokens_input": 100, "tokens_output": 50, "cost_usd": 0.01,
                },
                {
                    "id": "c", "trace_id": "agg-1", "parent_span_id": "p",
                    "name": "tool", "type": "tool",
                    "started_at": "2026-05-02T10:00:01Z",
                    "ended_at": "2026-05-02T10:00:02Z",
                    "tokens_input": 10, "tokens_output": 5, "cost_usd": 0.0001,
                },
            ]
        },
    )
    body = client.get("/v1/traces/agg-1").json()
    assert body["total_cost_usd"] == 0.0101
    # tokens: 100+50 + 10+5 = 165
    assert body["total_tokens"] == 165


def test_explicit_trace_total_overrides_smaller_aggregate(client):
    """When the user POSTs /v1/traces with an explicit total higher
    than the span sum, the explicit value wins (GREATEST semantics)."""
    client.post(
        "/v1/spans",
        json={
            "spans": [{
                "id": "s", "trace_id": "agg-2", "name": "x", "type": "llm",
                "started_at": "2026-05-02T10:00:00Z",
                "ended_at": "2026-05-02T10:00:01Z",
                "cost_usd": 0.001,
            }]
        },
    )
    client.post("/v1/traces", json={
        "id": "agg-2", "name": "manual",
        "started_at": "2026-05-02T10:00:00Z",
        "ended_at": "2026-05-02T10:00:01Z",
        "total_cost_usd": 99.99,
    })
    body = client.get("/v1/traces/agg-2").json()
    assert body["total_cost_usd"] == 99.99


def test_post_eval_creates_eval(client):
    _post_span(client, id="t", trace_id="t")
    response = client.post(
        "/v1/evals",
        json={
            "trace_id": "t",
            "name": "hallucination",
            "score": 0.91,
            "source": "llm_judge",
            "model": "gpt-4o-mini",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == "t"
    # FLOAT in DuckDB is 32-bit; expect approximate round-trip.
    assert abs(body["score"] - 0.91) < 1e-5
    assert body["id"]  # auto-assigned UUID


def test_traces_and_spans_isolated_per_trace_id(client):
    _post_span(client, id="a-root", trace_id="a", name="agent-a")
    _post_span(client, id="b-root", trace_id="b", name="agent-b")
    _post_span(
        client,
        id="a-child",
        trace_id="a",
        parent_span_id="a-root",
        name="a-child",
    )

    a_spans = client.get("/v1/traces/a/spans").json()
    b_spans = client.get("/v1/traces/b/spans").json()

    a_ids = {s["id"] for s in a_spans}
    b_ids = {s["id"] for s in b_spans}

    assert "a-root" in a_ids and "a-child" in a_ids
    assert b_ids == {"b-root"}
    assert a_ids.isdisjoint(b_ids)
