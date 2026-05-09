"""Telemetry-of-self (Slice 7B).

``/v1/admin/metrics`` — Lumin observing Lumin so operators spot a
degraded firewall before it lets attacks through.

What's surfaced (all derived from existing tables — no new schema):

  - decisions_total                Total decisions ever recorded
  - decisions_last_60s             Sliding window from decisions table
  - decisions_last_60s_by_verb     {block, flag, allow, ...}
  - decide_latency_ms              p50 / p95 / p99 from in-process
                                    ring buffer (policy_metrics)
  - traces_last_60s                Ingest-rate signal
  - spans_last_60s
  - webhook_dlq_total              Failures awaiting operator review
  - webhook_last_failure_at        Most recent dispatch failure
  - vault_entries_total            Cross-session vault size
  - approvals_pending              Stalled require_approval rows
  - policy_count_active            How many rules are loaded
  - latest_classifier_trained_at   Org classifier model age
  - panic_disabled                 Engine kill-switch state

Designed for Prometheus-style scraping: a single GET, JSON response,
no per-metric query (Lumin batches everything in one transaction).
A future export to Prometheus exposition format is an easy follow-up
— the JSON shape is deliberate for now (operators eyeball it via
``curl | jq``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from db import Database, get_db

logger = logging.getLogger("lumin.api.routers.metrics")

router = APIRouter()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_count(db: Database, sql: str, params: Optional[list] = None) -> int:
    try:
        row = db.fetchone(sql, params or [])
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        # Table may not exist on a fresh DB — return 0 not an error.
        return 0


def _safe_first(db: Database, sql: str, params: Optional[list] = None) -> Any:
    try:
        row = db.fetchone(sql, params or [])
        return row[0] if row else None
    except Exception:
        return None


@router.get("/v1/admin/metrics")
def admin_metrics(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Single-shot snapshot of every Lumin self-metric. Designed
    for periodic scraping (every 30s is plenty)."""
    now = _utc_now()
    sixty_s_ago = now - timedelta(seconds=60)

    # ---- decisions ---------------------------------------------------------
    decisions_total = _safe_count(db, "SELECT COUNT(*) FROM decisions")
    decisions_last_60s = _safe_count(
        db,
        "SELECT COUNT(*) FROM decisions WHERE decision_at >= ?",
        [sixty_s_ago],
    )
    by_verb_rows = []
    try:
        by_verb_rows = db.fetchall(
            """
            SELECT decision, COUNT(*) FROM decisions
            WHERE decision_at >= ?
            GROUP BY decision
            """,
            [sixty_s_ago],
        )
    except Exception:
        pass
    decisions_last_60s_by_verb = {
        (verb or "unknown"): int(n) for verb, n in by_verb_rows
    }

    # ---- decide latency (in-process ring buffer) ---------------------------
    # Reads from policy_metrics, which firewall.decide writes to via
    # record_eval() at the end of every decide call. The snapshot's
    # actual keys are eval_latency_ms_p50 / _p99 / _max — not the
    # _p95 / _p99_ms shorthand the original code looked for. Brutal-
    # test fix (2026-05-09).
    decide_latency: Dict[str, Any] = {
        "p50_ms": None, "p99_ms": None, "max_ms": None, "samples": 0,
    }
    try:
        import policy_metrics as _metrics
        snap = _metrics.snapshot().to_dict()
        decide_latency["p50_ms"] = snap.get("eval_latency_ms_p50")
        decide_latency["p99_ms"] = snap.get("eval_latency_ms_p99")
        decide_latency["max_ms"] = snap.get("eval_latency_ms_max")
        decide_latency["samples"] = snap.get("eval_latency_samples", 0)
    except Exception:
        pass

    # ---- ingest rate -------------------------------------------------------
    traces_last_60s = _safe_count(
        db,
        "SELECT COUNT(*) FROM traces WHERE ingest_at >= ?",
        [sixty_s_ago],
    )
    spans_last_60s = _safe_count(
        db,
        "SELECT COUNT(*) FROM spans WHERE started_at >= ?",
        [sixty_s_ago],
    )

    # ---- webhook health ----------------------------------------------------
    webhook_dlq_total = _safe_count(
        db, "SELECT COUNT(*) FROM firewall_webhook_failures",
    )
    webhook_last_failure_at = _safe_first(
        db,
        "SELECT MAX(failed_at) FROM firewall_webhook_failures",
    )
    webhook_last_fired_at = _safe_first(
        db,
        "SELECT MAX(last_fired_at) FROM firewall_webhooks",
    )
    webhook_count = _safe_count(
        db, "SELECT COUNT(*) FROM firewall_webhooks WHERE active = true",
    )

    # ---- vault -------------------------------------------------------------
    vault_entries_total = _safe_count(db, "SELECT COUNT(*) FROM session_vault")

    # ---- approvals ---------------------------------------------------------
    approvals_pending = _safe_count(
        db, "SELECT COUNT(*) FROM approvals WHERE state = 'pending'",
    )
    approvals_stale = _safe_count(
        db,
        """
        SELECT COUNT(*) FROM approvals
        WHERE state = 'pending'
          AND requested_at < ?
        """,
        [now - timedelta(hours=1)],
    )

    # ---- policies + panic --------------------------------------------------
    policy_count_active = _safe_count(
        db, "SELECT COUNT(*) FROM policies WHERE enabled = true",
    )
    panic_disabled = False
    panic_reason = None
    try:
        from firewall import decide as _fw_decide
        panic_disabled = bool(_fw_decide.is_panic_disabled())
    except Exception:
        pass
    try:
        row = db.fetchone(
            "SELECT v FROM firewall_kv WHERE k = 'panic_disabled'",
        )
        if row and row[0]:
            import json
            payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            panic_reason = payload.get("reason") if panic_disabled else None
    except Exception:
        pass

    # ---- classifier --------------------------------------------------------
    latest_classifier_trained_at = None
    classifier_count = 0
    try:
        latest_classifier_trained_at = _safe_first(
            db,
            """
            SELECT MAX(trained_at) FROM firewall_classifier_artifacts
            WHERE active = true
            """,
        )
        classifier_count = _safe_count(
            db, "SELECT COUNT(*) FROM firewall_classifier_artifacts WHERE active = true",
        )
    except Exception:
        pass

    return {
        "as_of": now.isoformat(),
        "decisions": {
            "total": decisions_total,
            "last_60s": decisions_last_60s,
            "last_60s_by_verb": decisions_last_60s_by_verb,
            "latency_ms": decide_latency,
        },
        "ingest": {
            "traces_last_60s": traces_last_60s,
            "spans_last_60s": spans_last_60s,
        },
        "webhooks": {
            "active_count": webhook_count,
            "dlq_total": webhook_dlq_total,
            "last_failure_at": (
                webhook_last_failure_at.isoformat()
                if isinstance(webhook_last_failure_at, datetime)
                else webhook_last_failure_at
            ),
            "last_fired_at": (
                webhook_last_fired_at.isoformat()
                if isinstance(webhook_last_fired_at, datetime)
                else webhook_last_fired_at
            ),
        },
        "vault": {
            "entries_total": vault_entries_total,
        },
        "approvals": {
            "pending": approvals_pending,
            "stale_over_1h": approvals_stale,
        },
        "policies": {
            "active": policy_count_active,
            "panic_disabled": panic_disabled,
            "panic_reason": panic_reason,
        },
        "classifier": {
            "models_active": classifier_count,
            "latest_trained_at": (
                latest_classifier_trained_at.isoformat()
                if isinstance(latest_classifier_trained_at, datetime)
                else latest_classifier_trained_at
            ),
        },
    }
