"""Trace replay — Slice 3 PR M / spec §5.10 + §14.1.

Replays a past trace through the current policy set and returns
what *would* have happened, span by span, decision by decision.

The three workflows this enables:

  1. **FP forecast before promoting shadow → enforce.** Operator
     promotes a rule from shadow to enforce. Replay against the
     last 30 days of traces shows how many decisions flip from
     "logged but not blocked" to "actually blocked" — and
     critically, on which traces, so the operator can audit
     before flipping.

  2. **Verify a new rule catches the historical incident.** When a
     new rule is authored (template, suggester, hand-written), the
     operator wants to check: does it actually fire on the trace
     that motivated it? Replay against that one trace + filter to
     just the new rule = answer in 200ms.

  3. **CI regression test.** A team can pin a small set of
     "golden" traces — known good, known bad — and replay them
     against current main. Any rule edit that flips a golden
     decision fails CI.

Implementation: load the trace's spans in chronological order,
synthesize a ``decide()`` call per span using the same lifecycle /
tool / params / output that produced it originally, with the
``policy_ids`` filter (if provided) restricting which rules apply.
The decisions returned are NOT persisted — replay is read-only.

Key constraint: the engine is per-process state (loaded YAML or DB
policies). Replay always uses the *current* engine — that's the
whole point. If an operator wants to replay against a candidate
policy they haven't yet committed, they can use the dashboard's
"Test against this trace" button on the editor (uses the same
endpoint with a one-off policy injected — landed in a follow-up).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from firewall import decide as fw_decide

logger = logging.getLogger("lumin.api.firewall.replay")


def replay_trace(
    db,
    trace_id: str,
    policy_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Replay ``trace_id`` through the current engine, optionally
    filtered to ``policy_ids``. Returns a structured response::

        {
          "trace_id": "...",
          "span_count": 12,
          "decisions": [
            {
              "span_id": "...",
              "lifecycle": "before_tool_call",
              "tool_name": "shell",
              "decision": "block",
              "policy_name": "owasp_llm06_destructive_shell",
              "reason": "rm -rf detected",
              "duration_ms": 1.4
            },
            ...
          ],
          "summary": {
            "block": 1,
            "rewrite": 0,
            "require_approval": 0,
            "flag": 2,
            "allow": 9
          }
        }

    Empty ``decisions`` is fine — a benign trace generates none.

    Per §10.1, replay does not persist decisions to the
    ``decisions`` table; that table holds the historical record of
    what *actually* happened. Replay output is always advisory.
    """
    if not trace_id:
        raise ValueError("trace_id is required")

    # Pull spans in chronological order. We need:
    #   - lifecycle (derived from span type + name; see below)
    #   - tool_name (for tool spans)
    #   - input + output text (for proxy / tool spans)
    #   - the request that produced it (for before-call replay)
    rows = db.fetchall_dict(
        """
        SELECT id, type, name, tool_name, input, output, started_at,
               metadata
        FROM spans
        WHERE trace_id = ?
        ORDER BY started_at ASC, id ASC
        """,
        [trace_id],
    )

    if not rows:
        raise KeyError(f"trace {trace_id!r} has no spans")

    # Pull the agent + project from the trace row so each replay
    # decide() call has the right routing context — same fields
    # the original decide saw.
    trace_row = db.fetchone_dict(
        "SELECT id, name, project FROM traces WHERE id = ?", [trace_id]
    )
    agent = trace_row.get("name") if trace_row else None
    project = trace_row.get("project") if trace_row else None

    decisions: List[Dict[str, Any]] = []
    summary = {"block": 0, "rewrite": 0, "require_approval": 0, "flag": 0, "allow": 0}

    # Filter: if policy_ids is set, fence the engine so only those
    # policies' decisions count. We do this AFTER calling decide()
    # because decide() doesn't expose a per-call policy filter today
    # — we just discard mismatched decisions in the response.
    policy_filter = set(policy_ids) if policy_ids else None

    for span in rows:
        for lifecycle in _lifecycles_for_span(span):
            try:
                resp = fw_decide.decide(
                    db,
                    lifecycle=lifecycle,
                    tool_name=span.get("tool_name"),
                    params=_params_from_span(span),
                    trace_id=trace_id,
                    span_id=span["id"],
                    agent=agent,
                    project=project,
                    output=_output_for_lifecycle(span, lifecycle),
                    persist=False,  # replay never writes to decisions table
                )
            except Exception:
                logger.exception(
                    "replay: decide() crashed on span %s lifecycle %s; skipping",
                    span.get("id"), lifecycle,
                )
                continue

            verb = resp.get("decision", "allow")
            policy_name = resp.get("policy_name")

            if policy_filter is not None:
                # Skip decisions whose matched policy isn't in the
                # filter set. ``allow`` with no matched policy is
                # ALSO skipped — the filter is "what would these
                # specific rules do".
                if not policy_name or policy_name not in policy_filter:
                    continue

            # Don't include the engine's "no rules matched at all"
            # allows — they're noise in the replay output.
            if verb == "allow" and not policy_name:
                continue

            decisions.append({
                "span_id": span["id"],
                "lifecycle": lifecycle,
                "tool_name": span.get("tool_name"),
                "decision": verb,
                "policy_name": policy_name,
                "reason": resp.get("reason"),
                "duration_ms": resp.get("duration_ms"),
            })
            if verb in summary:
                summary[verb] += 1

    return {
        "trace_id": trace_id,
        "span_count": len(rows),
        "decisions": decisions,
        "summary": summary,
    }


# ---- helpers --------------------------------------------------------------


def _lifecycles_for_span(span: Dict[str, Any]) -> List[str]:
    """Map a span row to the firewall lifecycles its existence
    implies. A tool span implies both ``before_tool_call`` and
    ``after_tool_call`` (we can't tell which fired the original
    decision without replaying both). A proxy / model span implies
    both ``before_proxy_call`` + ``after_proxy_call``.

    Why both directions: rules are lifecycle-scoped, so a span that
    historically passed ``before_tool_call`` rules cleanly might be
    caught by a *new* ``after_tool_call`` rule the operator just
    added. We need to surface those.
    """
    span_type = (span.get("type") or "").lower()
    if span_type == "tool":
        return ["before_tool_call", "after_tool_call"]
    if span_type in ("llm", "model", "proxy", "generation"):
        return ["before_proxy_call", "after_proxy_call"]
    # Generic span — only post_ingest applies (no proxy/tool semantics).
    return ["post_ingest"]


def _params_from_span(span: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reconstruct the ``params`` dict the original decide() saw
    for a tool span. Lumin records the raw input as a string in the
    span's ``input`` column; tool calls are JSON-shaped, so we try
    to parse. Falls back to ``{"raw": <text>}`` so rules with
    ``str(Input.params.get("command", ""))`` style conditions still
    have something useful to pattern-match on (since ``raw`` is the
    full original payload)."""
    raw = span.get("input")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        import json as _json
        parsed = _json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"raw": raw}
    except (ValueError, TypeError):
        return {"raw": raw}


def _output_for_lifecycle(
    span: Dict[str, Any], lifecycle: str
) -> Optional[Any]:
    """Output payload for the decide() call. Only meaningful for
    *after* lifecycles — the *before* ones see a placeholder None.

    Mirroring the runtime: ``Output.text`` is what the engine
    expressions use, so we wrap the span's output column in a dict
    when it's a string."""
    if lifecycle.startswith("before_"):
        return None
    out = span.get("output")
    if out is None:
        return None
    if isinstance(out, str):
        return {"text": out}
    return out
