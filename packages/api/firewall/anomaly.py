"""Behavioral anomaly detector — Slice 3 PR Q / spec §11.4.

Per-(agent, tool) rolling baselines. When the current call's
shape diverges sharply from the baseline, the detector returns a
high z-score — surfaceable as a policy condition::

    condition: behavioral_anomaly_score(tool_name, params, agent) > 4.0
    action: require_approval
    severity: medium

The intuition: a customer-support bot that's run ``shell`` 50 times
in the last 30 days, always with ``ls`` / ``cat`` / ``grep`` and
arg lengths around 30 chars, suddenly issues a ``shell`` call with
a 500-char arg. That's the deviation worth catching — well-known
attack shape after a successful prompt injection.

Baselines are derived from the existing ``spans`` table (no new
table needed). Computed on first call per (agent, tool) and cached
for ``ANOMALY_BASELINE_TTL_S`` seconds — cheap dynamic baselining
without a separate background job.

Three signals contribute to the score (max wins):

  1. **arg-length z-score**: how many stddevs is the current
     param payload size from the baseline mean?
  2. **arg-keys novelty**: did this call introduce param keys never
     seen for this (agent, tool) before? Each novel key adds 1.0
     to the floor.
  3. **frequency deviation**: how does the current session's call
     count for this tool compare to the per-session mean?

Combined score: ``max(arg_len_z, arg_keys_floor, freq_z)``. Returns
0.0 when baseline data is sparse (<MIN_BASELINE_SAMPLES samples)
— the agent is too new to score, treat anything as normal. Rule 7.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("lumin.api.firewall.anomaly")


MIN_BASELINE_SAMPLES = 10  # below this, we don't score (too noisy)
BASELINE_WINDOW_DAYS = 14
BASELINE_TTL_S = float(os.environ.get("LUMIN_ANOMALY_BASELINE_TTL", "300"))


# Process-local cache: (agent, tool) -> (timestamp, baseline_dict)
_BASELINE_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


def behavioral_anomaly_score(
    db,
    tool_name: Optional[str],
    params: Optional[Dict[str, Any]],
    agent: Optional[str],
    *,
    session_id: Optional[str] = None,
) -> float:
    """Return an anomaly score (0..∞-ish, capped at 100.0 in practice)
    for the current call shape against the (agent, tool) baseline.

    All inputs may be None / empty — returns 0.0 in that case (Rule 7).
    """
    if not tool_name or not agent:
        return 0.0
    try:
        baseline = _load_baseline(db, agent, tool_name)
        if baseline is None:
            return 0.0
        return _compute_score(baseline, params or {}, db, agent, tool_name, session_id)
    except Exception:
        logger.exception("anomaly: scoring failed")
        return 0.0


def _load_baseline(
    db, agent: str, tool_name: str
) -> Optional[Dict[str, Any]]:
    """Lazy-load + cache the baseline for an (agent, tool). None if
    fewer than MIN_BASELINE_SAMPLES historical calls exist."""
    key = (agent, tool_name)
    cached = _BASELINE_CACHE.get(key)
    if cached is not None:
        ts, payload = cached
        if time.monotonic() - ts < BASELINE_TTL_S:
            return payload

    cutoff = (datetime.now(timezone.utc) - timedelta(days=BASELINE_WINDOW_DAYS)).replace(tzinfo=None)
    rows = db.fetchall_dict(
        """
        SELECT s.input AS span_input, s.session_id AS session_id
        FROM spans s
        JOIN traces t ON t.id = s.trace_id
        WHERE t.name = ?
          AND s.tool_name = ?
          AND s.type = 'tool'
          AND s.started_at >= ?
        """,
        [agent, tool_name, cutoff],
    )
    if len(rows) < MIN_BASELINE_SAMPLES:
        _BASELINE_CACHE[key] = (time.monotonic(), None)  # type: ignore[assignment]
        return None

    arg_lengths: List[int] = []
    seen_keys: Set[str] = set()
    sessions: Dict[str, int] = {}
    for r in rows:
        raw = r.get("span_input")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            payload = None
        text_len = len(str(raw)) if raw is not None else 0
        arg_lengths.append(text_len)
        if isinstance(payload, dict):
            seen_keys.update(payload.keys())
        sid = r.get("session_id") or "_anon_"
        sessions[sid] = sessions.get(sid, 0) + 1

    arg_len_mean = _mean(arg_lengths)
    arg_len_std = _stddev(arg_lengths, arg_len_mean)
    freq_values = list(sessions.values())
    freq_mean = _mean(freq_values)
    freq_std = _stddev(freq_values, freq_mean)

    baseline = {
        "n_samples": len(rows),
        "arg_len_mean": arg_len_mean,
        "arg_len_std": arg_len_std or 1.0,  # avoid div0
        "seen_keys": seen_keys,
        "freq_mean": freq_mean,
        "freq_std": freq_std or 1.0,
    }
    _BASELINE_CACHE[key] = (time.monotonic(), baseline)
    return baseline


def _compute_score(
    baseline: Dict[str, Any],
    params: Dict[str, Any],
    db,
    agent: str,
    tool_name: str,
    session_id: Optional[str],
) -> float:
    """Score the current call against the baseline. Each signal
    independently produces a "how-anomalous" magnitude; we return
    the max so a sharp spike on any one axis surfaces."""
    # 1. arg-length z-score
    try:
        text = json.dumps(params, default=str) if params else ""
    except (TypeError, ValueError):
        text = str(params)
    cur_len = len(text)
    arg_len_z = abs(cur_len - baseline["arg_len_mean"]) / baseline["arg_len_std"]

    # 2. novel-keys floor — each new key adds 1.0
    novel_keys = 0
    if isinstance(params, dict):
        for k in params.keys():
            if k not in baseline["seen_keys"]:
                novel_keys += 1
    novel_floor = float(novel_keys)

    # 3. frequency z-score on this session, if session_id provided
    freq_z = 0.0
    if session_id:
        row = db.fetchone(
            """
            SELECT COUNT(*) FROM spans s
            JOIN traces t ON t.id = s.trace_id
            WHERE t.name = ? AND s.tool_name = ? AND s.session_id = ?
              AND s.type = 'tool'
            """,
            [agent, tool_name, session_id],
        )
        cur_count = int(row[0]) if row and row[0] is not None else 0
        freq_z = abs(cur_count - baseline["freq_mean"]) / baseline["freq_std"]

    score = max(arg_len_z, novel_floor, freq_z)
    # Cap at 100.0 — anything past 5 is already extreme. Keeps the
    # value interpretable in the dashboard.
    return min(score, 100.0)


def _mean(xs: List[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _stddev(xs: List[float], mean: float) -> float:
    if len(xs) < 2:
        return 0.0
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def reset_cache_for_tests() -> None:
    """Test helper — process-local cache is sticky across tests."""
    _BASELINE_CACHE.clear()
