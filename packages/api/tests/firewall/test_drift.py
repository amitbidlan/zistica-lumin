"""Tests for drift detection (Slice 3 PR F, §15.3)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from db import Database
from firewall import drift


@pytest.fixture(autouse=True)
def _reset():
    drift.reset_drift_for_tests()
    yield
    drift.reset_drift_for_tests()


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


def _seed(db: Database, *, policy: str, days_ago: int, count: int) -> None:
    """Seed `count` decision rows for `policy` on the day `days_ago` days
    ago. Used to build a daily count series for stddev.

    Anchors each day's rows to noon UTC of that day so the seeded
    rows can never spill across the UTC day boundary regardless of
    when the test runs (CI hitting 23:59:58 UTC was previously
    splitting today's 50 rows across two calendar days)."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    today_noon = now_utc.replace(hour=12, minute=0, second=0, microsecond=0)
    base_dt = today_noon - timedelta(days=days_ago)
    for i in range(count):
        decision_id = "dec_" + uuid.uuid4().hex[:24]
        db.execute(
            """
            INSERT INTO decisions (
                id, policy_id, policy_name, lifecycle, decision,
                mode_at_decision, reason, trace_id, span_id,
                session_id, agent, project, tool_name,
                matched_field, matched_value_truncated,
                decision_at, duration_ms, metadata
            ) VALUES (?, ?, ?, 'before_tool_call', 'block',
                      'enforce', 'r', NULL, NULL, NULL, NULL, NULL, 'exec',
                      NULL, NULL, ?, 1, NULL)
            """,
            [decision_id, policy, policy, base_dt + timedelta(seconds=i)],
        )


def test_detects_today_spike_against_quiet_baseline(db: Database):
    """Quiet for 13 days, then 50 fires today → drift alert."""
    # Seed 13 days of zero (no decisions written)
    # Today: 50 fires
    _seed(db, policy="spike_rule", days_ago=0, count=50)
    out = drift.detect_drift(db)
    assert len(out["alerts"]) == 1
    a = out["alerts"][0]
    assert a["policy_name"] == "spike_rule"
    assert a["today_count"] == 50
    assert a["direction"] == "up"


def test_no_alert_when_today_below_min_count(db: Database):
    """Today's count below MIN_COUNT_FOR_ALERT → no alert even on
    abnormal stddev. Avoids noise on rules that fired once today
    and zero times historically."""
    _seed(db, policy="quiet_rule", days_ago=0, count=2)  # below default 5
    out = drift.detect_drift(db)
    assert len(out["alerts"]) == 0


def test_no_alert_when_normal_within_baseline(db: Database):
    """Steady ~10 fires/day for 14 days — no alert."""
    for d in range(14):
        _seed(db, policy="steady_rule", days_ago=d, count=10)
    out = drift.detect_drift(db)
    assert len(out["alerts"]) == 0


def test_does_not_re_alert_same_day(db: Database):
    """Two runs on the same day → only one alert per policy."""
    _seed(db, policy="repeat_rule", days_ago=0, count=50)
    out1 = drift.detect_drift(db)
    assert len(out1["alerts"]) == 1
    out2 = drift.detect_drift(db)
    assert len(out2["alerts"]) == 0


def test_drift_alerts_table_persisted(db: Database):
    _seed(db, policy="persist_rule", days_ago=0, count=50)
    drift.detect_drift(db)
    rows = db.fetchall_dict("SELECT * FROM firewall_drift_alerts")
    assert len(rows) == 1


def test_acknowledge_marks_acknowledged_at(db: Database):
    _seed(db, policy="ack_rule", days_ago=0, count=50)
    out = drift.detect_drift(db)
    alert_id = out["alerts"][0]["id"]
    drift.acknowledge_alert(db, alert_id)
    rows = db.fetchall_dict(
        "SELECT acknowledged_at FROM firewall_drift_alerts WHERE id = ?",
        [alert_id],
    )
    assert rows[0]["acknowledged_at"] is not None


# ---- HTTP endpoints ------------------------------------------------------


def test_drift_run_endpoint(client, db):
    _seed(db, policy="ep_rule", days_ago=0, count=50)
    r = client.post("/v1/firewall/drift/run")
    assert r.status_code == 200
    assert len(r.json()["alerts"]) == 1


def test_drift_alerts_list_endpoint(client, db):
    _seed(db, policy="list_rule", days_ago=0, count=50)
    client.post("/v1/firewall/drift/run")
    r = client.get("/v1/firewall/drift/alerts")
    assert r.status_code == 200
    assert len(r.json()["alerts"]) >= 1


def test_drift_acknowledge_endpoint(client, db):
    _seed(db, policy="ack_ep_rule", days_ago=0, count=50)
    r1 = client.post("/v1/firewall/drift/run")
    aid = r1.json()["alerts"][0]["id"]
    r2 = client.post(f"/v1/firewall/drift/alerts/{aid}/acknowledge")
    assert r2.status_code == 200
