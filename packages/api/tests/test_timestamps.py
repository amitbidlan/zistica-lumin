"""Lock in UTC timestamp semantics across all auto-generated columns.

Without these, DuckDB's CURRENT_TIMESTAMP DEFAULT returns local time, which
is silently inconsistent with started_at/ended_at (stored as UTC).
"""

from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _close_to_utc_now(dt: datetime, slack_seconds: float = 5.0) -> bool:
    """A real UTC timestamp from the API should be within a few seconds of
    the test's UTC clock — NOT shifted by the local-TZ offset (would be
    minutes/hours off if CURRENT_TIMESTAMP was used)."""
    delta = abs((dt - _utc_now()).total_seconds())
    return delta < slack_seconds


def test_ingest_at_is_utc_for_root_span(client):
    span = {
        "id": "tz-root",
        "trace_id": "tz-root",
        "name": "x",
        "started_at": "2026-05-02T10:00:00Z",
    }
    client.post("/v1/spans", json={"spans": [span]})
    body = client.get("/v1/traces/tz-root").json()
    ingest_at = datetime.fromisoformat(body["ingest_at"])
    assert _close_to_utc_now(ingest_at), f"ingest_at {ingest_at} not close to UTC now"


def test_ingest_at_is_utc_for_orphan_stub(client):
    span = {
        "id": "child-tz",
        "trace_id": "stub-tz",
        "parent_span_id": "missing-root",
        "name": "child",
        "started_at": "2026-05-02T10:00:00Z",
    }
    client.post("/v1/spans", json={"spans": [span]})
    body = client.get("/v1/traces/stub-tz").json()
    ingest_at = datetime.fromisoformat(body["ingest_at"])
    assert _close_to_utc_now(ingest_at)


def test_ingest_at_is_utc_for_post_traces(client):
    client.post(
        "/v1/traces",
        json={
            "id": "manual-tz",
            "name": "manual",
            "started_at": "2026-05-02T10:00:00Z",
        },
    )
    body = client.get("/v1/traces/manual-tz").json()
    ingest_at = datetime.fromisoformat(body["ingest_at"])
    assert _close_to_utc_now(ingest_at)


def test_eval_created_at_is_utc(client):
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "ev-t",
                    "trace_id": "ev-t",
                    "name": "x",
                    "started_at": "2026-05-02T10:00:00Z",
                }
            ]
        },
    )
    response = client.post(
        "/v1/evals",
        json={"trace_id": "ev-t", "name": "h", "score": 0.5},
    )
    created_at = datetime.fromisoformat(response.json()["created_at"])
    assert _close_to_utc_now(created_at)
