"""Pattern suggester — Slice 3 PR D / spec §5.5 / §11.

Operator clicks "Block this pattern" on a fired decision in the
dashboard; this module extracts a regex from the matched value,
drafts a Policy YAML around it, runs a 30-day back-test against
the decisions table, and returns everything the dashboard needs to
let the operator review before saving.

The "compounding loop" the spec keeps talking about: every observed
violation can be promoted into a rule with one click. No regex
authoring, no Python — just review the draft, check the forecast,
save in shadow.

Design notes:

  - Pattern extraction is deliberately conservative. We escape the
    matched value as a literal regex rather than trying to
    generalize. Operators who want broader patterns edit the rule
    after promotion.

  - The back-test counts decisions where the same policy_name fired
    in the last 30 days, NOT a re-evaluation of the new condition
    against historical traces. The latter would require running
    the new rule across thousands of stored spans and is deferred
    to Slice 4 (where we have the trace replay infrastructure).

  - Suggestions are persisted in the ``pattern_suggestions`` table
    so the dashboard can list pending suggestions, an operator can
    revisit a draft they didn't immediately promote, and the
    frequent-pattern miner (PR E) can reuse the same storage.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from lumin.policy import Policy

import policy_store
from db import Database

logger = logging.getLogger("lumin.api.firewall.suggester")


# ---- public API ------------------------------------------------------------


def suggest_from_decision(db: Database, decision_id: str) -> Dict[str, Any]:
    """Build a draft Policy from a fired decision + persist it.

    Returns the suggestion dict (id, draft_yaml, rationale, forecast).
    Raises ``KeyError`` when decision_id doesn't exist.
    """
    decision = db.fetchone_dict(
        "SELECT * FROM decisions WHERE id = ?", [decision_id]
    )
    if decision is None:
        raise KeyError(f"decision not found: {decision_id}")

    draft = _draft_policy_from_decision(decision)
    rationale = _rationale(decision, draft)
    forecast = _forecast(db, decision)

    suggestion_id = "sug_" + uuid.uuid4().hex[:24]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.execute(
            """
            INSERT INTO pattern_suggestions (
                id, source_violation_id, template, draft_yaml, suggested_at,
                forecast_fp_count, forecast_fp_examples
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                suggestion_id, decision_id, "from_decision",
                _policy_to_yaml(draft), now,
                int(forecast["count"]),
                json.dumps(forecast["examples"]),
            ],
        )
    except Exception:
        # Persistence is best-effort — caller still gets the draft
        # even if we couldn't save the row (e.g. table missing on
        # an old install).
        logger.exception("suggester: failed to persist suggestion")

    return {
        "id": suggestion_id,
        "decision_id": decision_id,
        "template": "from_decision",
        "draft": _policy_to_dict(draft),
        "draft_yaml": _policy_to_yaml(draft),
        "rationale": rationale,
        "forecast": forecast,
    }


def get_suggestion(db: Database, suggestion_id: str) -> Optional[Dict[str, Any]]:
    """Re-fetch an existing suggestion from the table."""
    row = db.fetchone_dict(
        "SELECT * FROM pattern_suggestions WHERE id = ?", [suggestion_id]
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "decision_id": row.get("source_violation_id"),
        "template": row.get("template"),
        "draft_yaml": row.get("draft_yaml"),
        "promoted_to_policy_id": row.get("promoted_to_policy_id"),
        "dismissed_at": row.get("dismissed_at").isoformat() if row.get("dismissed_at") else None,
        "forecast": {
            "count": int(row.get("forecast_fp_count") or 0),
            "examples": _parse_examples(row.get("forecast_fp_examples")),
        },
    }


def promote_suggestion(
    db: Database, suggestion_id: str, name: str,
) -> Policy:
    """Turn a suggestion into a real Policy. Always lands in
    mode=shadow (§10.1). Marks the suggestion as promoted."""
    row = db.fetchone_dict(
        "SELECT * FROM pattern_suggestions WHERE id = ?", [suggestion_id]
    )
    if row is None:
        raise KeyError(f"suggestion not found: {suggestion_id}")
    if row.get("dismissed_at") is not None:
        raise ValueError("suggestion was dismissed — cannot promote")
    if row.get("promoted_to_policy_id"):
        raise ValueError(
            f"suggestion already promoted to {row['promoted_to_policy_id']!r}"
        )

    draft_yaml = row["draft_yaml"]
    policy = _policy_from_yaml(draft_yaml, override_name=name)
    saved = policy_store.create_policy(db, policy, actor="suggester")

    db.execute(
        "UPDATE pattern_suggestions SET promoted_to_policy_id = ? WHERE id = ?",
        [saved.name, suggestion_id],
    )
    return saved


def dismiss_suggestion(db: Database, suggestion_id: str) -> None:
    """Mark a suggestion as dismissed. Idempotent."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "UPDATE pattern_suggestions SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL",
        [now, suggestion_id],
    )


# ---- internals -------------------------------------------------------------


def _draft_policy_from_decision(decision: Dict[str, Any]) -> Policy:
    """Synthesize a Policy from a fired decision row."""
    lifecycle = str(decision.get("lifecycle") or "before_tool_call")
    tool_name = decision.get("tool_name")
    matched_value = (decision.get("matched_value_truncated") or "").strip()

    name = (
        f"suggested_block_{(tool_name or 'pattern')}_"
        f"{uuid.uuid4().hex[:6]}"
    )

    # Pattern: literal-escape the matched value so we don't get
    # unintended regex semantics. Operators broaden after promotion.
    if matched_value:
        # Truncate to keep the regex sane — first 80 chars are usually
        # the discriminating prefix.
        snippet = matched_value[:80]
        pattern = re.escape(snippet)
    else:
        pattern = "<no-matched-value>"

    # Build a condition that mirrors how the original decision fired.
    # For tool lifecycles we look at Input.params.command; for proxy
    # lifecycles we look at Output.text or Input.last_user_msg.
    if lifecycle in ("before_tool_call", "after_tool_call"):
        if tool_name:
            tool_clause = f'tool_name == "{tool_name}"'
        else:
            tool_clause = "True"
        condition = (
            f'{tool_clause} and regex_match('
            f'str(Input.params.get("command", "")), "{pattern}")'
        )
    elif lifecycle == "after_proxy_call":
        condition = f'regex_match(Output.text, "{pattern}")'
    elif lifecycle == "before_proxy_call":
        condition = f'regex_match(Input.last_user_msg, "{pattern}")'
    else:
        condition = f'regex_match(str(Input.text), "{pattern}")'

    return Policy(
        name=name,
        description=(
            f"Auto-generated from decision {decision['id'][:12]} — "
            f"blocks the literal pattern that fired the original "
            f"{decision.get('policy_name', 'rule')}."
        ),
        trigger="span_end",
        condition=condition,
        action="block",
        severity="medium",
        scope_agents=[],
        lifecycle=lifecycle,  # type: ignore[arg-type]
        mode="shadow",
        priority=50,
    )


def _rationale(decision: Dict[str, Any], draft: Policy) -> str:
    """Human-readable explanation shown above the draft in the modal."""
    return (
        f"Detected literal pattern from decision {decision['id'][:12]} "
        f"(originally fired by '{decision.get('policy_name', 'unknown')}'). "
        f"Drafting a {draft.action}-action rule on lifecycle "
        f"{draft.lifecycle} that re-fires on the same input shape."
    )


def _forecast(db: Database, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Cheap back-test: count past decisions from the same policy in
    last 30d. Real "would-have-fired against new condition" replay
    is Slice 4. Operators see this number as 'this is how often
    this pattern is showing up' rather than 'how many traces match
    your draft'.
    """
    pol = decision.get("policy_name")
    if not pol:
        return {"count": 0, "examples": []}
    try:
        cnt_row = db.fetchone(
            """
            SELECT COUNT(*) FROM decisions
            WHERE policy_name = ?
              AND decision_at >= NOW() - INTERVAL '30 days'
            """,
            [pol],
        )
        ex_rows = db.fetchall_dict(
            """
            SELECT trace_id FROM decisions
            WHERE policy_name = ?
              AND trace_id IS NOT NULL
              AND decision_at >= NOW() - INTERVAL '30 days'
            ORDER BY decision_at DESC
            LIMIT 5
            """,
            [pol],
        )
    except Exception:
        logger.exception("suggester: forecast query failed")
        return {"count": 0, "examples": []}
    return {
        "count": int(cnt_row[0]) if cnt_row else 0,
        "examples": [r["trace_id"] for r in ex_rows if r.get("trace_id")],
    }


def _policy_to_dict(p: Policy) -> Dict[str, Any]:
    return {
        "name": p.name,
        "description": p.description,
        "trigger": p.trigger,
        "condition": p.condition,
        "action": p.action,
        "severity": p.severity,
        "lifecycle": p.lifecycle,
        "mode": p.mode,
        "priority": p.priority,
    }


def _policy_to_yaml(p: Policy) -> str:
    """Serialize a Policy to a human-readable YAML string. We hand-
    write rather than yaml.safe_dump so the output stays compact +
    the field order is predictable for diffs."""
    lines = [
        f"name: {p.name}",
        f"description: {json.dumps(p.description or '')}",
        f"lifecycle: {p.lifecycle}",
        f"mode: {p.mode}",
        f"priority: {p.priority}",
        f"trigger: {p.trigger}",
        f"action: {p.action}",
        f"severity: {p.severity}",
        "condition: |",
    ]
    for ln in (p.condition or "").splitlines() or [""]:
        lines.append(f"  {ln}")
    return "\n".join(lines) + "\n"


def _policy_from_yaml(text: str, *, override_name: str) -> Policy:
    """Parse the text we wrote with _policy_to_yaml back into a
    Policy. Tolerates the description field being a JSON string."""
    import yaml as _yaml
    raw = _yaml.safe_load(text) or {}
    desc = raw.get("description")
    if isinstance(desc, str) and desc.startswith('"') and desc.endswith('"'):
        try:
            desc = json.loads(desc)
        except json.JSONDecodeError:
            pass
    return Policy(
        name=override_name,
        description=desc,
        trigger=raw.get("trigger", "span_end"),
        condition=raw.get("condition", ""),
        action=raw.get("action", "block"),
        severity=raw.get("severity", "medium"),
        scope_agents=[],
        lifecycle=raw.get("lifecycle", "post_ingest"),
        mode=raw.get("mode", "shadow"),
        priority=int(raw.get("priority", 0)),
    )


def _parse_examples(blob: Any) -> List[str]:
    if blob is None:
        return []
    if isinstance(blob, list):
        return [str(x) for x in blob if x]
    if isinstance(blob, str):
        try:
            v = json.loads(blob)
            if isinstance(v, list):
                return [str(x) for x in v if x]
        except json.JSONDecodeError:
            return []
    return []
