"""Tests for the telemetry-of-self endpoint (Slice 7B)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
import main


@pytest.fixture
def db() -> Generator[Database, None, None]:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture
def client(db: Database):
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_metrics_returns_complete_shape_on_empty_db(client: TestClient) -> None:
    """Fresh DB → every section present, all counters at 0,
    no fields raise."""
    resp = client.get("/v1/admin/metrics")
    assert resp.status_code == 200
    body = resp.json()
    for section in (
        "as_of", "decisions", "ingest", "webhooks", "vault",
        "approvals", "policies", "classifier",
    ):
        assert section in body, f"missing section: {section}"

    assert body["decisions"]["total"] == 0
    assert body["decisions"]["last_60s"] == 0
    assert body["decisions"]["last_60s_by_verb"] == {}
    assert body["ingest"]["traces_last_60s"] == 0
    assert body["webhooks"]["dlq_total"] == 0
    assert body["vault"]["entries_total"] == 0
    assert body["approvals"]["pending"] == 0


def test_metrics_counts_recent_decisions(client: TestClient, db: Database) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, verb in enumerate(["block", "block", "allow", "flag"]):
        db.execute(
            """
            INSERT INTO decisions (
                id, policy_id, policy_name, lifecycle, decision,
                mode_at_decision, decision_at, duration_ms
            ) VALUES (?, 'p', 'p', 'before_proxy_call', ?, 'enforce', ?, 1)
            """,
            [f"dec-{i}", verb, now - timedelta(seconds=10)],
        )
    resp = client.get("/v1/admin/metrics")
    body = resp.json()
    assert body["decisions"]["total"] == 4
    assert body["decisions"]["last_60s"] == 4
    assert body["decisions"]["last_60s_by_verb"]["block"] == 2
    assert body["decisions"]["last_60s_by_verb"]["allow"] == 1
    assert body["decisions"]["last_60s_by_verb"]["flag"] == 1


def test_metrics_excludes_old_decisions(client: TestClient, db: Database) -> None:
    """A decision recorded 5 minutes ago doesn't count toward
    last_60s. Total still counts."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        INSERT INTO decisions (
            id, policy_id, policy_name, lifecycle, decision,
            mode_at_decision, decision_at, duration_ms
        ) VALUES (?, 'p', 'p', 'before_proxy_call', 'block', 'enforce', ?, 1)
        """,
        ["old", now - timedelta(minutes=5)],
    )
    body = client.get("/v1/admin/metrics").json()
    assert body["decisions"]["total"] == 1
    assert body["decisions"]["last_60s"] == 0


def test_metrics_counts_traces_and_spans(client: TestClient, db: Database) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO traces (id, name, started_at, ingest_at) VALUES (?, ?, ?, ?)",
        ["t1", "agent_a", now - timedelta(seconds=20), now - timedelta(seconds=10)],
    )
    db.execute(
        "INSERT INTO spans (id, trace_id, started_at) VALUES (?, ?, ?)",
        ["s1", "t1", now - timedelta(seconds=20)],
    )
    body = client.get("/v1/admin/metrics").json()
    assert body["ingest"]["traces_last_60s"] == 1
    assert body["ingest"]["spans_last_60s"] == 1


def test_metrics_counts_pending_approvals(client: TestClient, db: Database) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    timeout_at = now + timedelta(minutes=10)
    for i, requested in enumerate([
        now - timedelta(minutes=5),    # recent
        now - timedelta(hours=2),      # stale (over 1h)
    ]):
        db.execute(
            """
            INSERT INTO approvals (
                id, decision_id, policy_id, state, requested_at,
                timeout_at, on_timeout
            ) VALUES (?, ?, ?, 'pending', ?, ?, 'allow')
            """,
            [f"apv-{i}", f"dec-{i}", "p", requested, timeout_at],
        )
    body = client.get("/v1/admin/metrics").json()
    assert body["approvals"]["pending"] == 2
    assert body["approvals"]["stale_over_1h"] == 1


def test_metrics_panic_state_surfaced(client: TestClient, db: Database) -> None:
    """When the panic kill-switch is on, metrics carries the flag
    + reason."""
    import json
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        INSERT INTO firewall_kv (k, v, updated_at, updated_by)
        VALUES ('panic_disabled', ?, ?, 'op')
        """,
        [json.dumps({"disabled": True, "reason": "false-positive"}), now],
    )
    # The runtime cache may or may not be in sync depending on test
    # ordering. The query against firewall_kv reads the persisted
    # state regardless. Force the in-process flag too so the test
    # is order-independent.
    from firewall import decide as _fw_decide
    _fw_decide.set_panic_disabled(True, reason="false-positive")
    try:
        body = client.get("/v1/admin/metrics").json()
        assert body["policies"]["panic_disabled"] is True
        assert body["policies"]["panic_reason"] == "false-positive"
    finally:
        _fw_decide.set_panic_disabled(False)


def test_metrics_webhook_dlq_count(client: TestClient, db: Database) -> None:
    """The webhook tables ship in Slice 4 PR #84 (feat/slice-4-webhooks);
    they're absent on a pre-#84 main. Create-if-not-exists so this
    test is order-independent across the merge queue."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_webhook_failures (
                id VARCHAR PRIMARY KEY,
                webhook_id VARCHAR NOT NULL,
                decision_id VARCHAR,
                attempt_count INTEGER NOT NULL,
                last_error VARCHAR,
                payload_truncated VARCHAR,
                failed_at TIMESTAMP NOT NULL
            )
            """
        )
    except Exception:
        # If something else failed (DuckDB schema parse), let the
        # later assertion fail with a real signal.
        pass
    db.execute(
        """
        INSERT INTO firewall_webhook_failures (
            id, webhook_id, decision_id, attempt_count, last_error,
            payload_truncated, failed_at
        ) VALUES ('f1', 'wh_x', 'dec_y', 3, 'simulated', '{}', ?)
        """,
        [now - timedelta(seconds=5)],
    )
    body = client.get("/v1/admin/metrics").json()
    assert body["webhooks"]["dlq_total"] == 1
    assert body["webhooks"]["last_failure_at"] is not None


def test_metrics_robust_to_missing_optional_tables(client: TestClient) -> None:
    """Even if classifier or vault tables are absent (older DB),
    the response is well-formed with zero counts (Rule 7)."""
    body = client.get("/v1/admin/metrics").json()
    assert "vault" in body
    assert "classifier" in body
    assert body["classifier"]["models_active"] == 0
