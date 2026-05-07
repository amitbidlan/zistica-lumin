"""Bypass log — Slice 3 PR P / spec §14.4.

Surfaces "things the firewall let through that an operator later
marked as bad". The pragmatic flip-side of the test harness: the
harness asks "do my rules catch what I think they should?"; the
bypass log asks "what did my rules miss?".

Two correlated bypass shapes the operator wants to find:

  1. **Label-driven** (this PR): a span in a trace was labeled
     ``bad`` by an operator (via PR #60's POST /v1/labels) but no
     firewall decision blocked / rewrote / required-approval on
     that span / trace. The label is the ground truth; absence of
     a matching block-class decision is the bypass.

  2. **Score-driven** (follow-up): a detector returned a score
     close to but below threshold (e.g. ``prompt_guard_score=0.65``
     when the rule fires at >0.7). Spec §14.4 calls this out as
     the canonical bypass shape. We don't ship it today because
     none of our detectors return ``{matched, score}`` from inside
     a decision call yet — refactor lands when we add the score
     plumbing (probably bundled with PR R / LLM judge).

The label-driven query is what's actually useful day-one because
the labels table already has data once PR #60 + the dashboard
labelling button are live. Score-driven is theoretical until
detectors emit per-call scores into the decisions row.

Queries:

  ``recent_bypasses(db, since=None, limit=50)``
    → list of bypass dicts joining labels ↔ decisions ↔ traces

  ``bypass_summary(db, since=None)``
    → {"total": N, "by_category": {...}, "by_policy_gap": [...]}
       Top "bypass categories" the operator should investigate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("lumin.api.firewall.bypass_log")


# Decision verbs that COUNT as "the firewall did something" — anything
# in this set on the same span/trace as a bad label means the firewall
# at least flagged the issue, so it's NOT a bypass. Pure ``allow`` (or
# no decision row) IS a bypass.
_BLOCKING_VERBS = ("block", "rewrite", "require_approval")


def recent_bypasses(
    db,
    since: Optional[datetime] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return labels marked ``bad`` whose owning span/trace had no
    block-class firewall decision. Ordered by label time DESC.

    "No block-class decision" means: there's no row in ``decisions``
    where ``trace_id == label.trace_id`` AND ``decision IN
    ('block', 'rewrite', 'require_approval')``. We allow the decision
    to be on any span of the trace, not just the labeled one — the
    operator's complaint is "this trace got through", and a sibling
    block on the same trace counts as catching it.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=30)
    # Database stores naive timestamps; strip tz for the comparison.
    since_naive = since.replace(tzinfo=None) if since.tzinfo else since

    # Fetch labels marked bad in the window; join in trace + span
    # context so the operator sees what they labeled. Then for each,
    # check whether the trace had a block-class decision — if not,
    # surface as a bypass.
    rows = db.fetchall_dict(
        """
        SELECT l.id AS label_id, l.trace_id, l.span_id,
               l.field, l.label, l.category, l.notes,
               l.labeled_by, l.labeled_at,
               t.name AS agent, t.project,
               s.tool_name, s.type AS span_type,
               s.input AS span_input, s.output AS span_output
        FROM labels l
        LEFT JOIN traces t ON t.id = l.trace_id
        LEFT JOIN spans s ON s.id = l.span_id
        WHERE l.label = 'bad'
          AND l.labeled_at >= ?
        ORDER BY l.labeled_at DESC
        LIMIT ?
        """,
        [since_naive, max(limit * 4, 200)],  # over-fetch — we filter further
    )

    out: List[Dict[str, Any]] = []
    for r in rows:
        trace_id = r.get("trace_id")
        if not trace_id:
            continue
        # Did anything block-class fire on this trace?
        blocked = db.fetchone(
            """
            SELECT id, policy_name, decision FROM decisions
            WHERE trace_id = ? AND decision IN ('block', 'rewrite', 'require_approval')
            ORDER BY decision_at ASC
            LIMIT 1
            """,
            [trace_id],
        )
        if blocked:
            # Firewall caught it — not a bypass.
            continue
        out.append({
            "label_id": r.get("label_id"),
            "trace_id": trace_id,
            "span_id": r.get("span_id"),
            "field": r.get("field"),
            "category": r.get("category"),
            "notes": r.get("notes"),
            "labeled_by": r.get("labeled_by"),
            "labeled_at": (
                r["labeled_at"].isoformat() if r.get("labeled_at") else None
            ),
            "agent": r.get("agent"),
            "project": r.get("project"),
            "tool_name": r.get("tool_name"),
            "span_type": r.get("span_type"),
            # Truncate I/O so the response stays small. Operators
            # can drill into the full trace for the rest.
            "span_input_preview": _truncate(r.get("span_input"), 500),
            "span_output_preview": _truncate(r.get("span_output"), 500),
        })
        if len(out) >= limit:
            break
    return out


def bypass_summary(
    db, since: Optional[datetime] = None
) -> Dict[str, Any]:
    """Aggregate bypass counts by label category and by tool name.
    Cheaper than ``recent_bypasses`` because it doesn't need to
    re-fetch span context."""
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=30)
    since_naive = since.replace(tzinfo=None) if since.tzinfo else since

    bypasses = recent_bypasses(db, since=since, limit=10_000)
    by_category: Dict[str, int] = {}
    by_tool: Dict[str, int] = {}
    by_agent: Dict[str, int] = {}
    for b in bypasses:
        cat = b.get("category") or "uncategorized"
        by_category[cat] = by_category.get(cat, 0) + 1
        tool = b.get("tool_name") or "n/a"
        by_tool[tool] = by_tool.get(tool, 0) + 1
        agent = b.get("agent") or "n/a"
        by_agent[agent] = by_agent.get(agent, 0) + 1
    return {
        "total": len(bypasses),
        "by_category": by_category,
        "by_tool": by_tool,
        "by_agent": by_agent,
    }


def _truncate(s: Any, n: int) -> Optional[str]:
    if s is None:
        return None
    text = str(s)
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."
