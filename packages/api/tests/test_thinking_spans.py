"""Tests for the new span_subtype + thinking_tokens columns."""


def test_span_subtype_and_thinking_tokens_round_trip(client):
    """A span posted with the new fields must come back with them
    intact via GET /v1/traces/{id}/spans."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "root-1", "trace_id": "t-thinking",
                    "name": "claude_call", "type": "llm",
                    "started_at": "2026-05-03T10:00:00Z",
                    "ended_at": "2026-05-03T10:00:03Z",
                    "model": "claude-opus-4-20250514",
                    "tokens_input": 50,
                    "tokens_output": 8500,
                    "thinking_tokens": 8000,
                },
                {
                    "id": "thinking-1", "trace_id": "t-thinking",
                    "parent_span_id": "root-1",
                    "name": "thinking", "type": "llm",
                    "started_at": "2026-05-03T10:00:00.1Z",
                    "ended_at": "2026-05-03T10:00:02.5Z",
                    "model": "claude-opus-4-20250514",
                    "thinking_tokens": 8000,
                    "span_subtype": "thinking",
                    "input": "let me think...",
                },
                {
                    "id": "response-1", "trace_id": "t-thinking",
                    "parent_span_id": "root-1",
                    "name": "response", "type": "llm",
                    "started_at": "2026-05-03T10:00:02.5Z",
                    "ended_at": "2026-05-03T10:00:03Z",
                    "span_subtype": "response",
                    "output": "the answer is 4",
                    "tokens_output": 500,
                },
            ]
        },
    )

    spans = client.get("/v1/traces/t-thinking/spans").json()
    by_name = {s["name"]: s for s in spans}

    assert by_name["thinking"]["span_subtype"] == "thinking"
    assert by_name["thinking"]["thinking_tokens"] == 8000
    assert "let me think" in by_name["thinking"]["input"]

    assert by_name["response"]["span_subtype"] == "response"
    assert by_name["response"]["thinking_tokens"] is None

    # Parent (claude_call) carries an aggregate thinking_tokens count
    assert by_name["claude_call"]["thinking_tokens"] == 8000


def test_existing_spans_default_to_null_subtype_and_thinking(client):
    """Span ingestion without the new fields must still succeed and
    return null for the new columns."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "plain-1", "trace_id": "plain-1",
                    "name": "ordinary_call",
                    "started_at": "2026-05-03T10:00:00Z",
                    "ended_at": "2026-05-03T10:00:01Z",
                }
            ]
        },
    )
    spans = client.get("/v1/traces/plain-1/spans").json()
    assert spans[0]["span_subtype"] is None
    assert spans[0]["thinking_tokens"] is None


def test_thinking_subtype_doesnt_affect_existing_aggregations(client):
    """The /v1/sessions aggregations should still work normally even
    when the spans table includes thinking_tokens / span_subtype."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "agg-1", "trace_id": "agg-1",
                    "name": "with_thinking",
                    "started_at": "2026-05-03T10:00:00Z",
                    "ended_at": "2026-05-03T10:00:02Z",
                    "session_id": "sess-thinking",
                    "tokens_input": 10, "tokens_output": 5000,
                    "thinking_tokens": 4500,
                    "total_cost_usd": 0.05,
                }
            ]
        },
    )
    sessions = client.get("/v1/sessions").json()
    by_id = {s["session_id"]: s for s in sessions}
    assert "sess-thinking" in by_id
    assert by_id["sess-thinking"]["trace_count"] == 1
