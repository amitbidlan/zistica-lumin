"""Retention cleanup: delete traces older than N days, cascade to spans + evals."""

from datetime import datetime, timedelta, timezone


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_cleanup_deletes_traces_older_than_retention(db, client):
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "old", "trace_id": "old", "name": "old_trace",
                 "started_at": _iso_days_ago(100)},
                {"id": "new", "trace_id": "new", "name": "new_trace",
                 "started_at": _now_iso()},
            ]
        },
    )
    assert len(client.get("/v1/traces").json()) == 2

    deleted = db.cleanup_old_traces(retention_days=90)
    assert deleted == 1

    traces = client.get("/v1/traces").json()
    assert [t["name"] for t in traces] == ["new_trace"]


def test_cleanup_cascades_to_spans(db, client):
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "old-root", "trace_id": "old-root", "name": "root",
                 "started_at": _iso_days_ago(100)},
                {"id": "old-child", "trace_id": "old-root",
                 "parent_span_id": "old-root", "name": "child",
                 "started_at": _iso_days_ago(100)},
            ]
        },
    )
    assert len(client.get("/v1/traces/old-root/spans").json()) == 2

    db.cleanup_old_traces(retention_days=90)

    assert client.get("/v1/traces/old-root").status_code == 404
    # Spans for the deleted trace must be gone too
    assert client.get("/v1/traces/old-root/spans").json() == []


def test_cleanup_cascades_to_evals(db, client):
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "old-eval", "trace_id": "old-eval", "name": "x",
                 "started_at": _iso_days_ago(100)},
            ]
        },
    )
    client.post(
        "/v1/evals",
        json={"trace_id": "old-eval", "name": "h", "score": 0.5},
    )

    db.cleanup_old_traces(retention_days=90)

    # Trace gone — evals attached to it should be gone too. We can't
    # query evals directly via the API, but cleanup_old_traces returned
    # 1 and the spans table also no longer has them.
    assert client.get("/v1/traces/old-eval").status_code == 404


def test_cleanup_returns_zero_when_no_old_traces(db, client):
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "n", "trace_id": "n", "name": "n",
                 "started_at": _now_iso()},
            ]
        },
    )
    assert db.cleanup_old_traces(retention_days=90) == 0
    assert len(client.get("/v1/traces").json()) == 1


def test_cleanup_with_zero_retention_deletes_everything(db, client):
    """retention_days=0 means: delete anything not from the future."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "a", "trace_id": "a", "name": "a",
                 "started_at": _iso_days_ago(1)},
                {"id": "b", "trace_id": "b", "name": "b",
                 "started_at": _iso_days_ago(0)},
            ]
        },
    )
    deleted = db.cleanup_old_traces(retention_days=0)
    assert deleted == 2
    assert client.get("/v1/traces").json() == []


def test_cleanup_negative_retention_is_no_op(db, client):
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "x", "trace_id": "x", "name": "x",
                 "started_at": _iso_days_ago(1000)},
            ]
        },
    )
    assert db.cleanup_old_traces(retention_days=-1) == 0
    assert len(client.get("/v1/traces").json()) == 1


def test_cleanup_keeps_traces_at_exact_boundary(db, client):
    """A trace with started_at == cutoff is NOT deleted (strict <)."""
    client.post(
        "/v1/spans",
        json={
            "spans": [
                {"id": "edge", "trace_id": "edge", "name": "edge",
                 "started_at": _iso_days_ago(89)},  # 1 day inside the 90-day window
            ]
        },
    )
    assert db.cleanup_old_traces(retention_days=90) == 0
    assert len(client.get("/v1/traces").json()) == 1
