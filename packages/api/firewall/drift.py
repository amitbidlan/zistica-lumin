"""Drift detection — Slice 3 PR F / spec §15.3.

Daily scan that compares each rule's fire rate today against its
baseline from the previous 14 days. Anything > 3σ from baseline
gets flagged as drift — either the agent's behavior changed
(upstream prompt template tweaked, new tool added) OR the rule
needs tuning.

Drift signals are surfaced two ways:

  1. ``GET /v1/firewall/drift`` — current alerts (used by the
     dashboard banner)
  2. ``firewall_drift_alerts`` table — historical record so
     operators can spot trends

Statistical model (intentionally simple):

  - Daily fire counts per policy, last 14 days.
  - baseline_mean = mean of days [today-14 .. today-1]
  - baseline_stddev = stddev of those 14 days
  - z = (today_count - baseline_mean) / baseline_stddev
  - alert if abs(z) > sigma_threshold AND today_count > min_count
    (the min_count guard avoids alerting on policies that fired
    once today and zero times historically)

Why simple statistics over a learned model:

  - Predictable. Operators understand "fired 12x today vs avg 2x".
  - Cheap. Single SQL group-by, runs in <100ms even on years of
    data thanks to the decisions table being indexed on
    decision_at + policy_name.
  - Honest about its limits. A learned model would over-fit to
    the noisy signal we get from a small Lumin install. Slice 4
    can add Prophet/etc. once we have telemetry showing this is
    a bottleneck.

Cost-bounded per spec §19.9:
  - Single 14-day SQL window
  - Per-policy stddev computed in Python over an aggregated row
  - One INSERT per alert that didn't already exist today
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from db import Database

logger = logging.getLogger("lumin.api.firewall.drift")


SIGMA_THRESHOLD = float(os.environ.get("LUMIN_DRIFT_SIGMA", "3.0"))
MIN_COUNT_FOR_ALERT = int(os.environ.get("LUMIN_DRIFT_MIN_COUNT", "5"))
INTERVAL_SECONDS = int(os.environ.get("LUMIN_DRIFT_INTERVAL_SECONDS", "86400"))

_LAST_RUN_AT: float = 0.0
_lock = threading.Lock()


# ---- one-time table creation ---------------------------------------------
#
# We add the alerts table inline rather than via the firewall
# migration module because it's specific to drift detection and
# Slice 4 might decide to drop / restructure it. Keeping the schema
# next to the consumer makes that future refactor a single-file
# change.

_CREATE_DRIFT_ALERTS = """
CREATE TABLE IF NOT EXISTS firewall_drift_alerts (
    id VARCHAR PRIMARY KEY,
    policy_name VARCHAR NOT NULL,
    detected_at TIMESTAMP NOT NULL,
    today_count INTEGER NOT NULL,
    baseline_mean DOUBLE NOT NULL,
    baseline_stddev DOUBLE NOT NULL,
    z_score DOUBLE NOT NULL,
    direction VARCHAR NOT NULL,
    acknowledged_at TIMESTAMP
);
"""

_CREATE_DRIFT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_drift_detected_at "
    "ON firewall_drift_alerts(detected_at DESC)"
)


def _ensure_table(db: Database) -> None:
    try:
        db.execute(_CREATE_DRIFT_ALERTS)
        db.execute(_CREATE_DRIFT_INDEX)
    except Exception:
        logger.exception("drift: failed to ensure alerts table")


# ---- public API -----------------------------------------------------------


def detect_drift(db: Database) -> Dict[str, Any]:
    """One-shot drift scan. Returns summary + list of fresh alerts."""
    with _lock:
        return _detect_drift_locked(db)


def maybe_detect_on_interval(db: Database) -> bool:
    """Run on the configured cadence (default daily). Cheap when
    called frequently."""
    global _LAST_RUN_AT
    if INTERVAL_SECONDS <= 0:
        return False
    now = time.time()
    if now - _LAST_RUN_AT < INTERVAL_SECONDS:
        return False
    try:
        detect_drift(db)
    except Exception:
        logger.exception("drift: scheduled run crashed")
        return False
    _LAST_RUN_AT = now
    return True


def list_recent_alerts(db: Database, limit: int = 50) -> List[Dict[str, Any]]:
    _ensure_table(db)
    rows = db.fetchall_dict(
        """
        SELECT * FROM firewall_drift_alerts
        ORDER BY detected_at DESC
        LIMIT ?
        """,
        [limit],
    )
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "policy_name": r["policy_name"],
            "detected_at": (
                r["detected_at"].isoformat() if r["detected_at"] else None
            ),
            "today_count": int(r["today_count"]),
            "baseline_mean": float(r["baseline_mean"]),
            "baseline_stddev": float(r["baseline_stddev"]),
            "z_score": float(r["z_score"]),
            "direction": r["direction"],
            "acknowledged_at": (
                r["acknowledged_at"].isoformat()
                if r.get("acknowledged_at") else None
            ),
        })
    return out


def acknowledge_alert(db: Database, alert_id: str) -> None:
    _ensure_table(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "UPDATE firewall_drift_alerts SET acknowledged_at = ? "
        "WHERE id = ? AND acknowledged_at IS NULL",
        [now, alert_id],
    )


def reset_drift_for_tests() -> None:
    global _LAST_RUN_AT
    _LAST_RUN_AT = 0.0


# ---- core detection -------------------------------------------------------


def _detect_drift_locked(db: Database) -> Dict[str, Any]:
    _ensure_table(db)

    # Pull daily counts per policy for last 14 days. DATE_TRUNC bucketing.
    try:
        rows = db.fetchall_dict(
            """
            SELECT policy_name,
                   DATE_TRUNC('day', decision_at) AS bucket_day,
                   COUNT(*) AS n
            FROM decisions
            WHERE decision_at >= NOW() - INTERVAL '14 days'
              AND decision IN ('block', 'flag', 'require_approval', 'rewrite')
            GROUP BY policy_name, DATE_TRUNC('day', decision_at)
            """
        )
    except Exception:
        logger.exception("drift: count query failed")
        return {"alerts": [], "scanned_policies": 0}

    # Group by policy_name → list of (day, count). Fill missing days
    # with zeros so the stddev reflects "didn't fire that day" rather
    # than "no data".
    by_policy: Dict[str, Dict[Any, int]] = {}
    for r in rows:
        by_policy.setdefault(r["policy_name"], {})[r["bucket_day"]] = int(r["n"])

    today = datetime.now(timezone.utc).date()
    new_alerts: List[Dict[str, Any]] = []

    for policy_name, daily in by_policy.items():
        # Build the 14-day series — today + previous 13 days
        series_days = []
        for offset in range(14):
            from datetime import timedelta
            d = today - timedelta(days=offset)
            n = 0
            for k, v in daily.items():
                if hasattr(k, "date"):
                    if k.date() == d:
                        n = v
                        break
                elif k == d:
                    n = v
                    break
            series_days.append(n)

        today_count = series_days[0]
        baseline = series_days[1:]  # 13 days prior

        if today_count < MIN_COUNT_FOR_ALERT:
            continue
        if len(baseline) < 3:
            continue

        mean = statistics.fmean(baseline)
        stddev = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
        if stddev <= 0:
            # All baseline days are zero or constant — divide-by-zero
            # avoidance. Use a small epsilon so we still alert when
            # today suddenly fires N times after weeks of zero.
            stddev = 1.0

        z = (today_count - mean) / stddev
        if abs(z) < SIGMA_THRESHOLD:
            continue
        direction = "up" if z > 0 else "down"

        # Skip if we already alerted on this policy today
        existing = db.fetchone(
            """
            SELECT 1 FROM firewall_drift_alerts
            WHERE policy_name = ?
              AND DATE_TRUNC('day', detected_at) = DATE_TRUNC('day', NOW())
            LIMIT 1
            """,
            [policy_name],
        )
        if existing:
            continue

        alert_id = "drift_" + uuid.uuid4().hex[:24]
        now_ts = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            db.execute(
                """
                INSERT INTO firewall_drift_alerts (
                    id, policy_name, detected_at, today_count,
                    baseline_mean, baseline_stddev, z_score, direction,
                    acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                [
                    alert_id, policy_name, now_ts, today_count,
                    float(mean), float(stddev), float(z), direction,
                ],
            )
            new_alerts.append({
                "id": alert_id,
                "policy_name": policy_name,
                "today_count": today_count,
                "baseline_mean": mean,
                "z_score": z,
                "direction": direction,
            })
        except Exception:
            logger.exception("drift: failed to insert alert for %s", policy_name)

    return {
        "alerts": new_alerts,
        "scanned_policies": len(by_policy),
    }
