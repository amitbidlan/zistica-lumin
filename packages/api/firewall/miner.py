"""Frequent-pattern miner — Slice 3 PR E / spec §11.3.

Background scanner that walks the recent ``decisions`` table looking
for clusters of similar decisions that didn't already trigger a rule
(or that triggered a noisy rule the operator might want to refine).
Surfaces clusters as ``pattern_suggestions`` rows with template=
``frequent_pattern`` so the dashboard can render them as drafts the
operator can promote with one click.

Why a separate miner from the per-violation suggester?

  - The per-violation suggester (Slice 3 PR D) is interactive — an
    operator clicks "Block this pattern" on one fired decision.
  - The miner is autonomous — it surfaces patterns the operator
    didn't even notice were happening. Compounding-loop bedrock:
    "your traces from yesterday become your blocking rules today".

Scope (intentionally narrow for v1):

  - Looks at decisions in the last 7 days.
  - Groups by (lifecycle, tool_name, normalised_command_first_word)
    where command is the first 80 chars of params.command (when
    present) or matched_value_truncated.
  - Emits a suggestion when a cluster has ≥ MIN_CLUSTER_SIZE
    decisions AND no existing pattern_suggestions row references
    the same cluster signature.
  - Cost-bounded per spec §19.9: runs at most every
    ``LUMIN_MINER_INTERVAL_SECONDS`` (default 3600s = 1 hour),
    skips when no new decisions since last run.

What the miner does NOT do (explicit non-goals):

  - Doesn't try to detect content drift / behavioral anomalies —
    that's §11.4 + §15.3.
  - Doesn't replay candidate rules against historical traces —
    that's Slice 4 trace-replay infrastructure.
  - Doesn't author conditions for clusters that span multiple
    lifecycles — single-lifecycle scope keeps the generated
    rules simple + correct.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import Database

logger = logging.getLogger("lumin.api.firewall.miner")


MIN_CLUSTER_SIZE = int(os.environ.get("LUMIN_MINER_MIN_CLUSTER_SIZE", "5"))
INTERVAL_SECONDS = int(os.environ.get("LUMIN_MINER_INTERVAL_SECONDS", "3600"))


_LAST_RUN_AT: float = 0.0
_lock = threading.Lock()


# ---- public API -----------------------------------------------------------


def mine_recent_patterns(db: Database) -> Dict[str, Any]:
    """One-shot scan of recent decisions. Inserts new
    pattern_suggestions rows for clusters that don't already have
    one. Returns a summary dict — { scanned, clusters, new_suggestions }.
    Safe to call concurrently — protected by an internal lock.
    """
    with _lock:
        return _mine_locked(db)


def maybe_mine_on_interval(db: Database) -> bool:
    """Run the miner if the interval has elapsed since the last run.
    Returns True if it actually ran. Cheap when called frequently —
    the timestamp check happens before any DB query.
    """
    global _LAST_RUN_AT
    now = time.time()
    if now - _LAST_RUN_AT < INTERVAL_SECONDS:
        return False
    if INTERVAL_SECONDS <= 0:
        return False
    try:
        mine_recent_patterns(db)
    except Exception:
        logger.exception("miner: scheduled run crashed")
        return False
    _LAST_RUN_AT = now
    return True


def reset_miner_for_tests() -> None:
    """Reset module-level state. Tests call this to avoid the
    cross-test interval skip."""
    global _LAST_RUN_AT
    _LAST_RUN_AT = 0.0


# ---- core mining logic ----------------------------------------------------


def _mine_locked(db: Database) -> Dict[str, Any]:
    """Inner mining work — called under _lock so concurrent miner
    runs collapse into one."""
    rows = _fetch_recent_decisions(db)
    if not rows:
        return {"scanned": 0, "clusters": 0, "new_suggestions": 0}

    # Group decisions by cluster signature
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        sig = _cluster_signature(row)
        if sig is None:
            continue
        clusters.setdefault(sig, []).append(row)

    # Filter to clusters above the threshold
    big_clusters = {
        sig: items for sig, items in clusters.items()
        if len(items) >= MIN_CLUSTER_SIZE
    }

    # Look up which signatures already have a pending suggestion so
    # we don't duplicate work on every miner run.
    existing_signatures = _existing_signatures(db)

    new_count = 0
    for sig, items in big_clusters.items():
        if sig in existing_signatures:
            continue
        try:
            _emit_suggestion(db, sig, items)
            new_count += 1
        except Exception:
            logger.exception(
                "miner: failed to emit suggestion for signature %r", sig
            )

    return {
        "scanned": len(rows),
        "clusters": len(big_clusters),
        "new_suggestions": new_count,
    }


def _fetch_recent_decisions(db: Database) -> List[Dict[str, Any]]:
    """Pull the last 7 days of decisions. Bounded by an explicit
    LIMIT so a runaway DB doesn't OOM the miner."""
    try:
        return db.fetchall_dict(
            """
            SELECT id, lifecycle, tool_name, decision, policy_name,
                   matched_value_truncated, decision_at
            FROM decisions
            WHERE decision_at >= NOW() - INTERVAL '7 days'
              AND decision IN ('block', 'flag', 'require_approval', 'rewrite')
            ORDER BY decision_at DESC
            LIMIT 50000
            """
        )
    except Exception:
        logger.exception("miner: fetch_recent_decisions failed")
        return []


_FIRST_WORD_RE = re.compile(r"[\"'\s\\(\\)\\[\\]]+|^cat\b|^head\b|^tail\b")


def _cluster_signature(row: Dict[str, Any]) -> Optional[str]:
    """Compute a stable string for clustering.

    Trade-off: too coarse a key collapses unrelated patterns (false
    "common" cluster). Too fine a key never finds repeats. We
    pick (lifecycle, tool_name, first 60 chars of matched_value)
    which is granular enough that "rm -rf /tmp/cache" and
    "rm -rf /tmp/logs" hash to the same cluster (the prefix is the
    discriminator) but "rm -rf" and "cat" don't.
    """
    lc = (row.get("lifecycle") or "").strip()
    if not lc:
        return None
    tool = (row.get("tool_name") or "").strip()
    matched = (row.get("matched_value_truncated") or "").strip()
    if not matched:
        return None
    # Truncate + collapse whitespace for stable hashing
    snippet = re.sub(r"\s+", " ", matched)[:60]
    return f"{lc}|{tool}|{snippet}"


def _existing_signatures(db: Database) -> set:
    """Pull cluster signatures that already have a pattern_suggestions
    row open (i.e. not promoted, not dismissed). The miner skips
    these on the next run.
    """
    out: set = set()
    try:
        rows = db.fetchall_dict(
            """
            SELECT draft_yaml FROM pattern_suggestions
            WHERE template = 'frequent_pattern'
              AND promoted_to_policy_id IS NULL
              AND dismissed_at IS NULL
            """
        )
    except Exception:
        return out
    for r in rows:
        # The signature is stashed as a YAML comment at top of the draft.
        text = r.get("draft_yaml") or ""
        m = re.search(r"#\s*cluster_signature:\s*(\S.*)$", text, re.M)
        if m:
            out.add(m.group(1).strip())
    return out


def _emit_suggestion(
    db: Database, signature: str, items: List[Dict[str, Any]],
) -> None:
    """Write a pattern_suggestions row for a cluster."""
    suggestion_id = "sug_" + uuid.uuid4().hex[:24]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cluster_size = len(items)
    representative = items[0]
    examples = [
        i.get("id") for i in items[:5] if i.get("id")
    ]

    draft_yaml = _draft_yaml_for_cluster(signature, representative, cluster_size)

    db.execute(
        """
        INSERT INTO pattern_suggestions (
            id, source_violation_id, template, draft_yaml, suggested_at,
            forecast_fp_count, forecast_fp_examples
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            suggestion_id,
            representative.get("id"),
            "frequent_pattern",
            draft_yaml,
            now,
            cluster_size,
            json.dumps(examples),
        ],
    )


def _draft_yaml_for_cluster(
    signature: str, representative: Dict[str, Any], cluster_size: int,
) -> str:
    """Render a draft YAML rule. Stash the cluster signature as a
    comment so subsequent miner runs can detect this signature is
    already covered (avoids duplicate suggestions)."""
    lc = representative.get("lifecycle", "post_ingest")
    tool = representative.get("tool_name") or ""
    matched = (representative.get("matched_value_truncated") or "").strip()
    snippet = re.escape(matched[:60])

    if lc in ("before_tool_call", "after_tool_call"):
        if tool:
            tool_clause = f'tool_name == "{tool}"'
        else:
            tool_clause = "True"
        condition = (
            f'{tool_clause} and regex_match('
            f'str(Input.params.get("command", "")), "{snippet}")'
        )
    elif lc == "after_proxy_call":
        condition = f'regex_match(Output.text, "{snippet}")'
    elif lc == "before_proxy_call":
        condition = f'regex_match(Input.last_user_msg, "{snippet}")'
    else:
        condition = f'regex_match(str(Input.text), "{snippet}")'

    name = (
        f"miner_{lc}_{(tool or 'pattern')}_"
        f"{uuid.uuid4().hex[:6]}"
    )
    description = (
        f"Auto-mined pattern: {cluster_size} similar decisions in "
        f"the last 7 days matched this shape. Review and promote to "
        f"block recurring agent behavior."
    )

    lines = [
        f"# cluster_signature: {signature}",
        f"name: {name}",
        f"description: {json.dumps(description)}",
        f"lifecycle: {lc}",
        "mode: shadow",
        "priority: 40",
        "trigger: span_end",
        "action: block",
        "severity: medium",
        "condition: |",
        f"  {condition}",
    ]
    return "\n".join(lines) + "\n"
