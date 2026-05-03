"""Tests for session aggregation endpoints — derived from traces table."""

from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _post_root(client, *, id_, session_id=None, name=None, started_at=None, ended_at=None, cost=None, tokens=0):
    return client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": id_, "trace_id": id_, "name": name or id_,
                    "started_at": started_at or _iso(datetime.now(timezone.utc)),
                    "ended_at": ended_at,
                    "session_id": session_id,
                }
            ]
        },
    )


def test_span_session_id_round_trips_through_api(client):
    """Regression: the spans table was missing a session_id column,
    so POSTs that included session_id had it silently dropped on
    the span row (only the trace metadata kept it). Confirm that
    GET /v1/traces/{id}/spans now returns the value."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "sess-roundtrip-1",
                    "trace_id": "sess-roundtrip-trace",
                    "name": "test",
                    "type": "custom",
                    "started_at": "2026-05-04T10:00:00Z",
                    "ended_at": "2026-05-04T10:00:01Z",
                    "session_id": "user-abc-conversation-123",
                },
                {
                    "id": "sess-roundtrip-2",
                    "trace_id": "sess-roundtrip-trace",
                    "parent_span_id": "sess-roundtrip-1",
                    "name": "child",
                    "type": "custom",
                    "started_at": "2026-05-04T10:00:00.500Z",
                    "ended_at": "2026-05-04T10:00:00.800Z",
                    "session_id": "user-abc-conversation-123",
                },
            ]
        },
    )
    spans = client.get("/v1/traces/sess-roundtrip-trace/spans").json()
    assert len(spans) == 2
    for s in spans:
        assert s["session_id"] == "user-abc-conversation-123", (
            f"{s['name']} span dropped session_id"
        )


def test_span_without_session_id_keeps_null(client):
    """A span without an explicit session_id should round-trip as
    null — not propagate from the trace's session_id."""
    client.post(
        "/v1/spans",
        json={
            "spans": [{
                "id": "no-sess-1",
                "trace_id": "no-sess-trace",
                "name": "test",
                "started_at": "2026-05-04T10:00:00Z",
                "ended_at": "2026-05-04T10:00:01Z",
            }]
        },
    )
    spans = client.get("/v1/traces/no-sess-trace/spans").json()
    assert spans[0]["session_id"] is None


def test_list_sessions_groups_traces_by_session_id(client):
    s1 = "session-A"
    s2 = "session-B"
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)

    _post_root(client, id_="a-1", session_id=s1, name="turn 1",
               started_at=_iso(base), ended_at=_iso(base + timedelta(seconds=2)))
    _post_root(client, id_="a-2", session_id=s1, name="turn 2",
               started_at=_iso(base + timedelta(seconds=10)), ended_at=_iso(base + timedelta(seconds=11)))
    _post_root(client, id_="b-1", session_id=s2, name="other",
               started_at=_iso(base), ended_at=_iso(base + timedelta(seconds=1)))

    response = client.get("/v1/sessions")
    assert response.status_code == 200
    sessions = response.json()
    by_id = {s["session_id"]: s for s in sessions}
    assert set(by_id) == {s1, s2}
    assert by_id[s1]["trace_count"] == 2
    assert by_id[s2]["trace_count"] == 1


def test_session_aggregations(client):
    sid = "agg-session"
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    _post_root(client, id_="x1", session_id=sid, name="t1",
               started_at=_iso(base), ended_at=_iso(base + timedelta(milliseconds=1500)))
    _post_root(client, id_="x2", session_id=sid, name="t2",
               started_at=_iso(base + timedelta(seconds=5)),
               ended_at=_iso(base + timedelta(seconds=5, milliseconds=2500)))

    sessions = client.get("/v1/sessions").json()
    s = next(x for x in sessions if x["session_id"] == sid)
    assert s["trace_count"] == 2
    # 1500 + 2500 ms summed
    assert s["total_duration_ms"] == 4000
    # Wall clock: from base to base+7.5s = 7500ms
    assert s["wall_duration_ms"] == 7500
    assert s["first_seen"].startswith("2026-05-01T10:00:00")
    assert s["last_seen"].startswith("2026-05-01T10:00:07")


def test_traces_without_session_id_are_excluded(client):
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    _post_root(client, id_="orphan", session_id=None, name="standalone",
               started_at=_iso(base))
    sessions = client.get("/v1/sessions").json()
    ids = {s["session_id"] for s in sessions}
    assert "orphan" not in ids
    # Empty session_id (string) also excluded
    _post_root(client, id_="empty", session_id="", name="empty",
               started_at=_iso(base))
    sessions = client.get("/v1/sessions").json()
    ids = {s["session_id"] for s in sessions}
    assert "" not in ids


def test_get_session_returns_traces_in_chronological_order(client):
    sid = "ordered-session"
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    _post_root(client, id_="t-late", session_id=sid, name="last",
               started_at=_iso(base + timedelta(seconds=10)))
    _post_root(client, id_="t-early", session_id=sid, name="first",
               started_at=_iso(base))
    _post_root(client, id_="t-mid", session_id=sid, name="middle",
               started_at=_iso(base + timedelta(seconds=5)))

    detail = client.get(f"/v1/sessions/{sid}").json()
    names = [t["name"] for t in detail["traces"]]
    assert names == ["first", "middle", "last"]
    assert detail["trace_count"] == 3
    assert detail["session_id"] == sid


def test_get_session_404(client):
    response = client.get("/v1/sessions/does-not-exist")
    assert response.status_code == 404


def test_session_id_propagates_from_root_span(client):
    """The SDK posts a span with session_id; the trace upsert must store it."""
    sid = "propagation-test"
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "p-root", "trace_id": "p-root", "name": "agent",
                    "started_at": "2026-05-01T10:00:00Z",
                    "ended_at": "2026-05-01T10:00:01Z",
                    "session_id": sid,
                }
            ]
        },
    )
    body = client.get("/v1/traces/p-root").json()
    assert body["session_id"] == sid


def test_session_id_propagates_from_orphan_child_to_stub(client):
    """A child span arriving before its root creates a stub trace; the
    session_id should land on the stub so the trace is visible in the
    session aggregation right away."""
    sid = "orphan-session"
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "orphan-child", "trace_id": "stub-trace",
                    "parent_span_id": "missing-root", "name": "child",
                    "started_at": "2026-05-01T10:00:00Z",
                    "session_id": sid,
                }
            ]
        },
    )
    body = client.get("/v1/traces/stub-trace").json()
    assert body["session_id"] == sid
    sessions = client.get("/v1/sessions").json()
    ids = {s["session_id"] for s in sessions}
    assert sid in ids


def test_root_span_does_not_overwrite_existing_session_id(client):
    """If a stub already has session_id and a root arrives WITHOUT one,
    keep the stub's session_id (COALESCE behavior)."""
    sid = "preserved-session"
    # Orphan child carries session_id
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "child-y", "trace_id": "preserve-trace",
                    "parent_span_id": "root-y", "name": "child",
                    "started_at": "2026-05-01T10:00:00Z",
                    "session_id": sid,
                }
            ]
        },
    )
    # Root arrives without session_id
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "preserve-trace", "trace_id": "preserve-trace",
                    "name": "root_agent",
                    "started_at": "2026-05-01T10:00:00Z",
                    "ended_at": "2026-05-01T10:00:01Z",
                }
            ]
        },
    )
    body = client.get("/v1/traces/preserve-trace").json()
    assert body["session_id"] == sid


def test_pagination(client):
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _post_root(
            client, id_=f"pg-{i}",
            session_id=f"session-{i:02d}",
            name=f"t{i}",
            started_at=_iso(base + timedelta(minutes=i)),
        )
    response = client.get("/v1/sessions?limit=2")
    assert len(response.json()) == 2

    response = client.get("/v1/sessions?limit=2&offset=2")
    assert len(response.json()) == 2
