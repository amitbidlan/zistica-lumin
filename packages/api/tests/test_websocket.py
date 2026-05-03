"""Tests for the /ws/traces WebSocket fanout."""

import json

import pytest
from fastapi.testclient import TestClient


def test_websocket_connect_and_disconnect_cleanly(client):
    """Just opening and closing a connection must not error."""
    with client.websocket_connect("/ws/traces") as ws:
        # connection accepted; nothing else expected
        assert ws is not None


def test_new_span_broadcast_on_ingest(client):
    """Ingesting a span should push a `new_span` message to subscribers."""
    with client.websocket_connect("/ws/traces") as ws:
        client.post(
            "/v1/spans",
            json={
                "spans": [
                    {
                        "id": "ws-1",
                        "trace_id": "ws-1",
                        "name": "my_agent",
                        "started_at": "2026-05-02T10:00:00Z",
                        "ended_at": "2026-05-02T10:00:01Z",
                    }
                ]
            },
        )
        # We expect TWO messages for a root span: new_span + new_trace
        msgs = [json.loads(ws.receive_text()) for _ in range(2)]
        types = {m["type"] for m in msgs}
        assert types == {"new_span", "new_trace"}

        span_msg = next(m for m in msgs if m["type"] == "new_span")
        assert span_msg["trace_id"] == "ws-1"
        assert span_msg["span"]["id"] == "ws-1"
        assert span_msg["span"]["name"] == "my_agent"

        trace_msg = next(m for m in msgs if m["type"] == "new_trace")
        assert trace_msg["trace"]["id"] == "ws-1"
        assert trace_msg["trace"]["name"] == "my_agent"


def test_non_root_span_only_emits_new_span_for_existing_trace(client):
    """Once a trace exists, subsequent spans on it only emit `new_span`."""
    # Pre-create the trace via a root span
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "root", "trace_id": "root", "name": "root",
                    "started_at": "2026-05-02T10:00:00Z",
                }
            ]
        },
    )

    with client.websocket_connect("/ws/traces") as ws:
        client.post(
            "/v1/spans",
            json={
                "spans": [
                    {
                        "id": "child", "trace_id": "root",
                        "parent_span_id": "root", "name": "child",
                        "started_at": "2026-05-02T10:00:00.5Z",
                    }
                ]
            },
        )
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "new_span"
        assert msg["trace_id"] == "root"
        assert msg["span"]["parent_span_id"] == "root"


def test_orphan_child_emits_new_trace_for_stub(client):
    """A non-root span arriving before its root creates a stub trace —
    that should also fire `new_trace` so the dashboard knows about it."""
    with client.websocket_connect("/ws/traces") as ws:
        client.post(
            "/v1/spans",
            json={
                "spans": [
                    {
                        "id": "orphan-child", "trace_id": "stub-trace",
                        "parent_span_id": "missing", "name": "child",
                        "started_at": "2026-05-02T10:00:00Z",
                    }
                ]
            },
        )
        msgs = [json.loads(ws.receive_text()) for _ in range(2)]
        types = {m["type"] for m in msgs}
        assert types == {"new_span", "new_trace"}


def test_root_span_after_stub_re_emits_new_trace(client):
    """When an orphan child creates a stub and the root arrives later,
    the root should re-broadcast `new_trace` so the dashboard updates
    the cached row from stub (name=null) to fully populated."""
    # 1. Orphan child first — creates stub trace
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "child-1", "trace_id": "race-trace",
                    "parent_span_id": "race-trace", "name": "child",
                    "started_at": "2026-05-02T10:00:00.5Z",
                }
            ]
        },
    )

    # 2. Now subscribe and post the root
    with client.websocket_connect("/ws/traces") as ws:
        client.post(
            "/v1/spans",
            json={
                "spans": [
                    {
                        "id": "race-trace", "trace_id": "race-trace",
                        "name": "root_agent",
                        "started_at": "2026-05-02T10:00:00Z",
                        "ended_at": "2026-05-02T10:00:01Z",
                    }
                ]
            },
        )
        msgs = [json.loads(ws.receive_text()) for _ in range(2)]
        types = {m["type"] for m in msgs}
        assert types == {"new_span", "new_trace"}, (
            "root span on existing stub must re-emit new_trace so dashboard "
            "updates name from null"
        )
        trace_msg = next(m for m in msgs if m["type"] == "new_trace")
        assert trace_msg["trace"]["name"] == "root_agent"


def test_multiple_subscribers_all_receive_broadcast(client):
    """Two simultaneous WS clients should both receive the same span."""
    with client.websocket_connect("/ws/traces") as a:
        with client.websocket_connect("/ws/traces") as b:
            client.post(
                "/v1/spans",
                json={
                    "spans": [
                        {
                            "id": "fanout", "trace_id": "fanout", "name": "f",
                            "started_at": "2026-05-02T10:00:00Z",
                        }
                    ]
                },
            )
            # Each client gets new_span + new_trace
            a_msgs = [json.loads(a.receive_text()) for _ in range(2)]
            b_msgs = [json.loads(b.receive_text()) for _ in range(2)]
            for msgs in (a_msgs, b_msgs):
                assert {m["type"] for m in msgs} == {"new_span", "new_trace"}


def test_ingest_succeeds_when_no_subscribers(client):
    """No connected clients ⇒ broadcast is a no-op, ingest still works."""
    response = client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "no-subs", "trace_id": "no-subs", "name": "x",
                    "started_at": "2026-05-02T10:00:00Z",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 1}


def test_ingest_unaffected_when_ws_loop_unavailable(client):
    """If the loop reference is missing (no lifespan), ingest still must
    succeed — broadcast just no-ops."""
    from ws import manager
    saved_loop = manager._loop
    manager._loop = None
    try:
        response = client.post(
            "/v1/spans",
            json={
                "spans": [
                    {
                        "id": "no-loop", "trace_id": "no-loop", "name": "x",
                        "started_at": "2026-05-02T10:00:00Z",
                    }
                ]
            },
        )
        assert response.status_code == 200
    finally:
        manager._loop = saved_loop
