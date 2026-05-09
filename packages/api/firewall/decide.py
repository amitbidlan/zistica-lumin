"""Synchronous decision engine — heart of the Agent Firewall.

Implements §5.1 (`POST /v1/policy/decide`) and §10.1–10.3 (mode,
panic disable, circuit breaker). Stateless and import-safe; the HTTP
layer in ``routers/firewall.py`` owns request/response shapes and
DB connection plumbing.

Decision flow for a single request:

  1. Honor the global panic kill-switch — if set, return allow with
     reason="panic_disabled" and skip the engine entirely. (§10.2)
  2. Filter policies by ``lifecycle == request.lifecycle``, by
     ``circuit_breaker_state == 'ok'``, and (when ``request.agent``
     is set) by scope_agents membership.
  3. Sort by ``priority DESC`` so high-priority policies see the
     request first. Ties broken by name for determinism.
  4. Evaluate each policy's condition with a fresh simpleeval. The
     namespace is built from the request payload + the firewall
     builtins from ``firewall.builtins`` (stateless + history-backed).
  5. First non-allow decision wins; an explicit ``allow`` short-
     circuits remaining lower-priority policies.
  6. Apply mode: ``shadow`` records the decision but returns allow;
     ``flag`` records and returns flag; ``enforce`` records and
     returns the policy's action verbatim.
  7. Hard timeout per lifecycle (§2.4). If the wall clock exceeds
     the budget mid-loop, abort and return allow (Rule 7 — agent
     never blocks on Lumin).

Every error path returns allow. Internal exceptions are logged but
swallowed; the agent that called us never fails because of us.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from lumin.policy import Policy

import policy_store
from db import Database

from firewall import builtins as fw_builtins

logger = logging.getLogger("lumin.api.firewall.decide")


# ---- valid decision values ------------------------------------------------

VALID_DECISIONS = frozenset({
    "allow", "block", "flag", "require_approval", "rewrite",
})


# Per-lifecycle wall-clock budget in milliseconds. From §2.4 of the
# spec: B/C 50ms, D 100ms, E 300ms. post_ingest has no real-time
# budget but we cap it at 500ms so a runaway condition can't pin a
# request thread.
LATENCY_BUDGET_MS: Dict[str, int] = {
    "before_proxy_call": 50,
    "after_proxy_call": 300,
    "before_tool_call": 50,
    "after_tool_call": 100,
    "post_ingest": 500,
}


# ---- session-level deny cache (Slice 2 Tier 1.5(b)) ----------------------
#
# When an operator denies a tool call, the LLM doesn't always
# learn — Slice 1 dogfood (2026-05-07) saw an agent retry the same
# rm -rf three times after a deny, asking the user to approve via
# fake /approve syntax each time. The cache short-circuits this:
# once a (session_id, tool_name, params_hash) is denied, subsequent
# decide() calls for the same tuple auto-deny in <1ms without
# touching the policy engine OR re-prompting the operator.
#
# Cache scope is intentionally narrow:
#   - Per-session: a deny for session A doesn't affect session B
#   - Per-tool: blocking shell rm -rf doesn't affect a fetch call
#   - Per-params-hash: the same exact command. A *different* shell
#     command with different params is re-evaluated normally
#     (operators have to approve each variation, not "approve all
#     shell commands forever for this session").
#
# TTL: 600s by default (matches require_approval timeout). Override
# with LUMIN_DENY_CACHE_TTL=N (seconds, 0 disables).
#
# Storage: in-memory dict, keyed by (session_id, tool_name, params_hash).
# Sized loosely — 100k entries is ~30MB at peak; we don't bound size
# yet because real-world session counts are bounded by retention
# (90d default) and each session's denied tuples are typically <10.

import hashlib
import os

_DENY_CACHE: Dict[tuple, float] = {}
_DENY_CACHE_TTL = float(os.environ.get("LUMIN_DENY_CACHE_TTL", "600"))


def _params_hash(params: Optional[Dict[str, Any]]) -> str:
    """Stable hash of tool params for cache keying. Order-independent
    so {a:1,b:2} and {b:2,a:1} cache as the same call. Truncated
    to 16 hex chars — collision probability is fine at this scope."""
    if not params:
        return "noparams"
    try:
        payload = json.dumps(params, sort_keys=True, default=str)
    except Exception:
        payload = str(params)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _deny_cache_key(
    session_id: Optional[str],
    tool_name: Optional[str],
    params: Optional[Dict[str, Any]],
) -> Optional[tuple]:
    """Build the cache key tuple, or None when this request shouldn't
    use the cache (no session = no cross-call correlation possible)."""
    if not session_id or not tool_name:
        return None
    return (str(session_id), str(tool_name), _params_hash(params))


def _check_deny_cache(key: Optional[tuple]) -> Optional[str]:
    """Return the policy_name that previously denied this tuple if
    a non-expired entry exists, else None. Lazily evicts expired
    entries on lookup so the cache size stays bounded by recent
    activity."""
    if not key or _DENY_CACHE_TTL <= 0:
        return None
    entry = _DENY_CACHE.get(key)
    if entry is None:
        return None
    expires_at, policy_name = entry  # type: ignore[misc]
    if time.time() > expires_at:
        _DENY_CACHE.pop(key, None)
        return None
    return str(policy_name)


def _record_deny_in_cache(
    key: Optional[tuple], policy_name: str,
) -> None:
    """Cache a deny for TTL seconds. No-op when the key is missing
    (e.g. session_id wasn't supplied) or TTL is disabled."""
    if not key or _DENY_CACHE_TTL <= 0:
        return
    _DENY_CACHE[key] = (time.time() + _DENY_CACHE_TTL, policy_name)  # type: ignore[assignment]


def reset_deny_cache_for_tests() -> None:
    """Test helper. The cache is per-process module global; tests
    that exercise the cache flow should clear between cases."""
    _DENY_CACHE.clear()


# ---- panic kill-switch ----------------------------------------------------

# In-memory flag flipped by POST /v1/firewall/panic_disable. Persisted
# to the DB on flip so the state survives restart; the value here is a
# best-effort cache, refreshed when the panic endpoint runs and on
# explicit ``refresh_panic_state(db)`` calls.
_PANIC_DISABLED: bool = False
_PANIC_REASON: Optional[str] = None


def is_panic_disabled() -> bool:
    return _PANIC_DISABLED


def set_panic_disabled(disabled: bool, reason: Optional[str] = None) -> None:
    """Local toggle. The HTTP endpoint also writes to the DB so a
    restart picks the state back up via ``refresh_panic_state``."""
    global _PANIC_DISABLED, _PANIC_REASON
    _PANIC_DISABLED = bool(disabled)
    _PANIC_REASON = reason if disabled else None


def refresh_panic_state(db: Database) -> None:
    """Reload the panic flag from DB. Called on startup + after the
    HTTP handler writes a new state. Errors fall back to disabled=False
    rather than raising — Rule 7 applies even to our own bookkeeping.
    """
    try:
        row = db.fetchone(
            "SELECT v FROM firewall_kv WHERE k = ?", ["panic_disabled"]
        )
        if row and row[0]:
            payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            set_panic_disabled(bool(payload.get("disabled")), payload.get("reason"))
        else:
            set_panic_disabled(False, None)
    except Exception:
        # Table may not exist yet (pre-migration), or row missing.
        # Either way: leave the flag as-is.
        pass


# ---- auto circuit breaker (Tier 4.1) -------------------------------------
#
# Track per-policy fire counts in a rolling 60-second window. When a
# single policy fires more than ``_AUTO_TRIP_FIRES_PER_MINUTE`` times
# in 60s, flip its ``circuit_breaker_state`` to 'tripped' so subsequent
# evaluations skip it. The trip is sticky until an operator un-trips
# the policy via the dashboard / API — we don't auto-reset, because a
# rule that fires 100+/min is by definition broken or under attack
# and a human needs to look.
#
# Set ``LUMIN_AUTO_TRIP_FIRES_PER_MINUTE=0`` to disable the auto-trip
# entirely (operators may want this when running a known-noisy rule
# in shadow mode while gathering tuning data).

_AUTO_TRIP_FIRES_PER_MINUTE = int(
    os.environ.get("LUMIN_AUTO_TRIP_FIRES_PER_MINUTE", "100")
)

# In-memory tracker. Keyed by policy.name → deque of monotonic
# timestamps (seconds). Each fire appends ``time.time()``; entries
# older than 60s are trimmed lazily on each track / check call.
_RULE_FIRE_RATES: Dict[str, Deque[float]] = {}


def _now_seconds() -> float:
    """Wall-clock time, monkeypatchable in tests."""
    return time.time()


def _track_fire(policy_name: str) -> None:
    """Record that ``policy_name`` just fired. Trims the window to the
    most recent 60 seconds.
    """
    if not policy_name:
        return
    now = _now_seconds()
    cutoff = now - 60.0
    dq = _RULE_FIRE_RATES.get(policy_name)
    if dq is None:
        dq = deque()
        _RULE_FIRE_RATES[policy_name] = dq
    dq.append(now)
    # Trim old entries — pop from the left while the oldest is stale.
    while dq and dq[0] < cutoff:
        dq.popleft()


def _check_circuit_breaker(policy_name: str) -> bool:
    """Return True iff ``policy_name`` has fired more than
    ``_AUTO_TRIP_FIRES_PER_MINUTE`` times in the last 60 seconds and
    should be auto-tripped.

    Returns False when the threshold env var is 0 (auto-trip disabled).
    """
    if _AUTO_TRIP_FIRES_PER_MINUTE <= 0:
        return False
    if not policy_name:
        return False
    dq = _RULE_FIRE_RATES.get(policy_name)
    if not dq:
        return False
    # Trim before counting so a stale window doesn't trigger a false
    # trip after a long quiet period.
    cutoff = _now_seconds() - 60.0
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq) > _AUTO_TRIP_FIRES_PER_MINUTE


def _maybe_auto_trip(db: Database, policy_name: str) -> None:
    """Track + check + (if breached) flip the policy's circuit_breaker_state
    to 'tripped' in the DB. Errors are logged and swallowed — Rule 7."""
    try:
        _track_fire(policy_name)
        if not _check_circuit_breaker(policy_name):
            return
        # Threshold breached: trip.
        try:
            policy_store.update_policy(
                db, policy_name, circuit_breaker_state="tripped"
            )
        except Exception:
            # Don't crash the engine if the DB write fails — log and
            # continue. The next call will re-attempt because the
            # rolling window still has the over-threshold count.
            logger.exception(
                "firewall.decide: failed to persist auto-trip for policy %r",
                policy_name,
            )
            return
        n = len(_RULE_FIRE_RATES.get(policy_name, ()))
        logger.warning(
            "auto-tripped policy %s — fired %d times in the last minute",
            policy_name, n,
        )
        # Clear the deque so we don't re-trip on every call after the
        # threshold (the policy is already tripped; the next decide()
        # will skip it via _applicable_policies).
        _RULE_FIRE_RATES.pop(policy_name, None)
    except Exception:
        logger.exception(
            "firewall.decide: auto-trip bookkeeping crashed for policy %r",
            policy_name,
        )


def reset_fire_rate_tracker_for_tests() -> None:
    """Clear the per-policy fire-rate tracker. Tests must call this
    between cases so fires from a previous test don't leak into the
    next one's window."""
    _RULE_FIRE_RATES.clear()


# ---- agent_feedback helper (Slice 2 Tier 1.5(a)) -------------------------
#
# When Lumin denies a tool call or LLM output, OpenClaw passes our
# decision through to the agent as a tool error string. Slice 1
# dogfood (2026-05-07) captured a live LLM reasoning trace that
# revealed four distinct misunderstandings:
#
#   1. The LLM thought the deny came from the user, not a separate
#      enforcement layer ("Denied by user").
#   2. It treated policy as user-controlled ("user gave permission?").
#   3. It hallucinated a fake approval prompt and showed it to the
#      user (`/approve rm -rf <command>`) — a social engineering
#      surface.
#   4. The error string was opaque — Lumin's policy_name + reason
#      never reached the LLM's reasoning context.
#
# ``agent_feedback`` is a string the plugin v0.4.x reads off our
# response and surfaces verbatim as the tool error. It's authored
# to be:
#   - Authoritative (cites policy name)
#   - Disambiguating ("platform-level, not user")
#   - Anti-hallucination ("Do NOT generate approval syntax")
#   - Action-directing ("Reply to the user that...")
#
# We keep it concise so it doesn't blow the LLM's response token
# budget when echoed in tool errors.


def _agent_feedback(
    *, decision: str, policy_name: Optional[str], reason: str,
) -> str:
    """Construct the LLM-targeted feedback string for a non-allow
    decision. Returned in the decide response body so the plugin
    can surface it as the tool error / proxy error verbatim.

    Tuned for the failure modes we observed in 2026-05-07 dogfood:
    LLM hallucinating /approve syntax, retrying after deny, mis-
    attributing to user permission. Each clause targets one of
    those failure modes.
    """
    pol = f" (policy: {policy_name})" if policy_name else ""
    if decision == "block":
        return (
            f"This action was blocked by the Lumin Agent Firewall{pol}. "
            f"Reason: {reason}. This is a platform-level denial enforced "
            f"regardless of user request — it is NOT a user decision and "
            f"cannot be overridden by asking the user. "
            f"Do NOT retry this command. Do NOT generate /approve syntax "
            f"or any other approval prompt to the user. "
            f"Reply that you cannot perform this action due to security "
            f"policy and stop."
        )
    if decision == "require_approval":
        return (
            f"This action requires operator approval per the Lumin Agent "
            f"Firewall{pol}. Reason: {reason}. The approval request has "
            f"already been routed to a configured operator channel — DO "
            f"NOT ask the end user to approve, and DO NOT generate "
            f"/approve syntax in your reply. While waiting, simply tell "
            f"the user you're checking on the request."
        )
    if decision == "rewrite":
        return (
            f"This action was modified by the Lumin Agent Firewall{pol} "
            f"to redact sensitive data. Reason: {reason}. Continue with "
            f"the rewritten parameters as provided. Do not attempt to "
            f"reconstruct the original."
        )
    if decision == "flag":
        return (
            f"This action was flagged by the Lumin Agent Firewall{pol} "
            f"for operator review. Reason: {reason}. The action was "
            f"allowed to proceed; no additional response action required."
        )
    return reason


# ---- core decide ----------------------------------------------------------


def decide(
    db: Database,
    *,
    lifecycle: str,
    tool_name: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    session_id: Optional[str] = None,
    agent: Optional[str] = None,
    project: Optional[str] = None,
    model: Optional[str] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    output: Optional[Any] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Run the firewall against a single decision request.

    Returns the response dict per spec §5.1. Never raises.

    When ``persist=False``, the decision is NOT written to the
    ``decisions`` / ``approvals`` tables — used by the replay
    endpoint (§5.10) and the rule unit-test harness (§14.3). All
    side effects that touch persistent state are skipped:
      - decisions row insert
      - approvals row insert (require_approval -> dummy id)
      - deny-cache writes
      - auto-circuit-breaker fire tracking
    """
    t0 = time.perf_counter()
    # Local recorder honours `persist` — replay/test paths see
    # decision_id=None and no DB writes happen for the decision row.
    def _record(*args, **kwargs) -> Optional[str]:
        if not persist:
            return None
        return _record_decision(*args, **kwargs)
    budget_ms = LATENCY_BUDGET_MS.get(lifecycle, 500)
    deadline = t0 + (budget_ms / 1000.0)

    # ---- Rule 7 fast paths ------------------------------------------------
    # Panic kill-switch — short-circuit before touching any policy state.
    if is_panic_disabled():
        return _allow_response(
            t0, reason="panic_disabled", panic_reason=_PANIC_REASON
        )

    if lifecycle not in LATENCY_BUDGET_MS:
        # Unknown lifecycle = caller bug, not our problem. Allow.
        return _allow_response(t0, reason=f"unknown_lifecycle:{lifecycle}")

    # ---- Session-level deny cache (Slice 2 Tier 1.5(b)) -----------------
    # Once an operator denies a (session, tool, params) tuple, the
    # LLM frequently retries the same call. Auto-deny in <1ms without
    # re-bothering the operator (or running the full engine again).
    cache_key = _deny_cache_key(session_id, tool_name, params)
    cached_policy = _check_deny_cache(cache_key)
    if cached_policy is not None:
        decision_id = _record(
            db, policy=None, lifecycle=lifecycle,
            decision="block", reason=f"cached_deny:{cached_policy}",
            trace_id=trace_id, span_id=span_id, session_id=session_id,
            agent=agent, project=project, tool_name=tool_name,
            duration_ms=_elapsed_ms(t0),
            mode_at_decision="enforce",
        )
        return {
            "decision": "block",
            "policy_id": cached_policy,
            "policy_name": cached_policy,
            "reason": (
                f"This action was previously denied in this session and is "
                f"auto-denied for the deny-cache window."
            ),
            "agent_feedback": _agent_feedback(
                decision="block",
                policy_name=cached_policy,
                reason="previously denied in this session — do not retry",
            ),
            "decision_id": decision_id,
            "mode_at_decision": "enforce",
            "duration_ms": _elapsed_ms(t0),
            "cached_deny": True,
        }

    try:
        policies = _applicable_policies(db, lifecycle=lifecycle, agent=agent)
    except Exception:
        logger.exception("firewall.decide: policy load failed")
        return _allow_response(t0, reason="internal_error")

    # No applicable rules — fast allow. Skips the simpleeval / history
    # builtin setup entirely so the cold path stays under 1ms.
    if not policies:
        return _allow_response(t0)

    namespace = _build_namespace(
        tool_name=tool_name,
        params=params,
        trace_id=trace_id,
        span_id=span_id,
        session_id=session_id,
        agent=agent,
        project=project,
        model=model,
        messages=messages,
        output=output,
    )

    try:
        funcs = _build_functions(db)
    except Exception:
        logger.exception("firewall.decide: function table build failed")
        return _allow_response(t0, reason="internal_error")

    # Track whether any rule fired in shadow/flag mode so we can
    # surface a hint in the allow response (the dashboard uses this
    # to show "would have blocked" badges on traces).
    shadow_hits: List[Dict[str, Any]] = []

    for policy in policies:
        # Hard timeout — Rule 7. Better an unblocked agent than a
        # stuck one.
        if time.perf_counter() > deadline:
            logger.warning(
                "firewall.decide: timeout (%dms budget) at policy %s",
                budget_ms, policy.name,
            )
            _record(
                db, policy=None, lifecycle=lifecycle,
                decision="allow", reason=f"timeout_{budget_ms}ms",
                trace_id=trace_id, span_id=span_id, session_id=session_id,
                agent=agent, project=project, tool_name=tool_name,
                duration_ms=_elapsed_ms(t0), mode_at_decision="n/a",
            )
            return _allow_response(t0, reason=f"timeout_{budget_ms}ms")

        try:
            triggered = _evaluate(policy, namespace, funcs)
        except Exception:
            logger.exception(
                "firewall.decide: policy %r evaluation crashed", policy.name
            )
            on_err = getattr(policy, "on_internal_error", "allow")
            if on_err == "deny":
                # Operator chose deny-on-error for this policy
                # explicitly — log and treat as a block.
                decision_id = _record(
                    db, policy=policy, lifecycle=lifecycle,
                    decision="block", reason="internal_error",
                    trace_id=trace_id, span_id=span_id, session_id=session_id,
                    agent=agent, project=project, tool_name=tool_name,
                    duration_ms=_elapsed_ms(t0),
                    mode_at_decision=policy.mode,
                )
                return {
                    "decision": "block",
                    "policy_id": policy.name,
                    "policy_name": policy.name,
                    "reason": "internal_error",
                    "agent_feedback": _agent_feedback(
                        decision="block",
                        policy_name=policy.name,
                        reason="internal_error",
                    ),
                    "decision_id": decision_id,
                    "mode_at_decision": policy.mode,
                    "duration_ms": _elapsed_ms(t0),
                }
            # Default Rule 7: skip and continue.
            continue

        if not triggered:
            continue

        # Map policy.action → response decision. Unknown actions =
        # noop. Cancel/alert (legacy post_ingest actions) get mapped
        # to flag for synchronous lifecycles so they don't 500.
        action = (policy.action or "").lower()
        if action in ("alert", "cancel"):
            action = "flag"
        if action == "allow":
            # Explicit allow short-circuits — record + return.
            decision_id = _record(
                db, policy=policy, lifecycle=lifecycle,
                decision="allow", reason="policy_allow",
                trace_id=trace_id, span_id=span_id, session_id=session_id,
                agent=agent, project=project, tool_name=tool_name,
                duration_ms=_elapsed_ms(t0),
                mode_at_decision=policy.mode,
            )
            return {
                "decision": "allow",
                "policy_id": policy.name,
                "policy_name": policy.name,
                "reason": policy.description or "policy_allow",
                "decision_id": decision_id,
                "mode_at_decision": policy.mode,
                "duration_ms": _elapsed_ms(t0),
            }

        if action not in ("block", "flag", "require_approval", "rewrite"):
            # Unrecognized action — log once and skip.
            logger.warning(
                "firewall.decide: policy %r has unknown action %r — skipping",
                policy.name, policy.action,
            )
            continue

        # Auto circuit breaker — Tier 4.1. Track this fire and, if the
        # policy has fired too often in the last 60s, flip it to
        # 'tripped' in the DB. The trip applies to *subsequent* calls
        # (this call's decision is honored). _applicable_policies
        # already filters out tripped policies on the next request.
        # Skipped on replay (persist=False) — replay shouldn't trip
        # real production circuits.
        if persist:
            _maybe_auto_trip(db, policy.name)

        # ---- mode resolution --------------------------------------------
        # shadow → record and continue (don't return, lower-priority
        # rules might still want to flag/block in real mode); flag →
        # always return decision='flag' regardless of action; enforce
        # → return the policy's action.
        mode = (policy.mode or "enforce").lower()
        reason = policy.description or f"policy:{policy.name}"

        if mode == "shadow":
            decision_id = _record(
                db, policy=policy, lifecycle=lifecycle,
                decision=action, reason=reason,
                trace_id=trace_id, span_id=span_id, session_id=session_id,
                agent=agent, project=project, tool_name=tool_name,
                duration_ms=_elapsed_ms(t0),
                mode_at_decision="shadow",
            )
            shadow_hits.append({
                "policy_id": policy.name,
                "would_have_been": action,
                "decision_id": decision_id,
            })
            continue

        if mode == "flag":
            decision_id = _record(
                db, policy=policy, lifecycle=lifecycle,
                decision="flag", reason=reason,
                trace_id=trace_id, span_id=span_id, session_id=session_id,
                agent=agent, project=project, tool_name=tool_name,
                duration_ms=_elapsed_ms(t0),
                mode_at_decision="flag",
            )
            return {
                "decision": "flag",
                "policy_id": policy.name,
                "policy_name": policy.name,
                "reason": reason,
                "agent_feedback": _agent_feedback(
                    decision="flag", policy_name=policy.name, reason=reason,
                ),
                "decision_id": decision_id,
                "mode_at_decision": "flag",
                "duration_ms": _elapsed_ms(t0),
            }

        # mode == enforce — first non-allow rule wins.
        decision_id = _record(
            db, policy=policy, lifecycle=lifecycle,
            decision=action, reason=reason,
            trace_id=trace_id, span_id=span_id, session_id=session_id,
            agent=agent, project=project, tool_name=tool_name,
            duration_ms=_elapsed_ms(t0),
            mode_at_decision="enforce",
        )

        if action == "require_approval":
            # Replay (persist=False) doesn't create real approvals —
            # the dashboard would surface a phantom row. The replay
            # response still says decision="require_approval"; the
            # caller sees the policy fired without a side effect.
            approval_id = (
                _create_approval(
                    db, decision_id=decision_id, policy=policy,
                    trace_id=trace_id, agent=agent, tool_name=tool_name,
                    params=params,
                )
                if persist
                else None
            )
            return {
                "decision": "require_approval",
                "policy_id": policy.name,
                "policy_name": policy.name,
                "reason": reason,
                "agent_feedback": _agent_feedback(
                    decision="require_approval",
                    policy_name=policy.name, reason=reason,
                ),
                "approval_id": approval_id,
                "timeout_s": 600,
                "decision_id": decision_id,
                "mode_at_decision": "enforce",
                "duration_ms": _elapsed_ms(t0),
            }

        if action == "rewrite":
            rewritten = _redact_for_rewrite(params, output)
            return {
                "decision": "rewrite",
                "rewritten": rewritten,
                "policy_id": policy.name,
                "policy_name": policy.name,
                "reason": reason,
                "agent_feedback": _agent_feedback(
                    decision="rewrite", policy_name=policy.name, reason=reason,
                ),
                "decision_id": decision_id,
                "mode_at_decision": "enforce",
                "duration_ms": _elapsed_ms(t0),
            }

        # action == block | flag
        return {
            "decision": action,
            "policy_id": policy.name,
            "policy_name": policy.name,
            "reason": reason,
            "agent_feedback": _agent_feedback(
                decision=action, policy_name=policy.name, reason=reason,
            ),
            "decision_id": decision_id,
            "mode_at_decision": "enforce",
            "duration_ms": _elapsed_ms(t0),
        }

    # Loop exhausted with no enforce/flag hit. Return allow with any
    # shadow_hits so the caller (and the dashboard) can show "would
    # have blocked" telemetry.
    return _allow_response(t0, shadow_hits=shadow_hits or None)


# ---- helpers --------------------------------------------------------------


def _applicable_policies(
    db: Database, *, lifecycle: str, agent: Optional[str]
) -> List[Policy]:
    """Read enabled policies matching this lifecycle, agent scope, and
    not currently tripped. Sort by priority DESC, name ASC.

    The cheap path: a single ``SELECT * FROM policies WHERE enabled
    AND lifecycle = ? AND circuit_breaker_state = 'ok'``. We can't push
    the agent-scope check into SQL because scope_agents is JSON-encoded;
    do it in Python.
    """
    rows = db.fetchall_dict(
        """
        SELECT * FROM policies
        WHERE enabled = true
          AND lifecycle = ?
          AND circuit_breaker_state = 'ok'
        """,
        [lifecycle],
    )
    out: List[Policy] = []
    for r in rows:
        try:
            p = policy_store._row_to_policy(r)
        except Exception:
            logger.exception(
                "firewall.decide: skipping malformed policy row %r", r.get("name")
            )
            continue
        if not p.applies_to_agent(agent):
            continue
        out.append(p)
    out.sort(key=lambda p: (-int(getattr(p, "priority", 0) or 0), p.name))
    return out


def _build_namespace(**req: Any) -> Dict[str, Any]:
    """The simpleeval ``names`` table — what condition expressions can
    reference. Mirrors what an Input/Output/Tool span sees, so policies
    written for post_ingest read identically when promoted to a
    synchronous lifecycle.
    """
    params = req.get("params") or {}
    messages = req.get("messages") or []
    # Compose a flattened "input text" — convenient for regex builtins
    # that don't want to walk a nested dict. Joins all string values
    # in params + last user message.
    parts: List[str] = []
    for v in (params or {}).values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v if isinstance(x, (str, int, float)))
    last_user_msg = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                last_user_msg = content
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        last_user_msg = blk.get("text", "")
    output_text = req.get("output") if isinstance(req.get("output"), str) else ""

    return {
        "Input": _NS({
            "tool_name": req.get("tool_name"),
            "params": params,
            "text": " ".join(parts),
            "messages": messages,
            "last_user_msg": last_user_msg,
            "model": req.get("model"),
            "agent": req.get("agent"),
            "project": req.get("project"),
            "session_id": req.get("session_id"),
            "trace_id": req.get("trace_id"),
            "span_id": req.get("span_id"),
        }),
        "Output": _NS({
            "text": output_text,
            "raw": req.get("output"),
        }),
        # Bare names for shorthand expressions like
        # ``looks_like_secret(text)`` — these resolve to whichever side
        # of the pipe is the most likely target.
        "text": " ".join(parts) if parts else (output_text or last_user_msg),
        "tool_name": req.get("tool_name"),
        "params": params,
    }


class _NS:
    """Attribute-access wrapper around a dict — gives policy
    expressions ``Input.params.command`` semantics without having to
    teach simpleeval about chained dict lookups.

    Also exposes ``.get(key, default)`` so existing dict-shaped
    conditions like ``Input.params.get("command", "")`` keep working
    without rewrites — that pattern is used across the OWASP starter
    pack and saves operators from having to learn two access styles.
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name == "_data":
            raise AttributeError(name)
        # ``.get`` is intercepted here rather than defined as a method
        # so it doesn't shadow a real param/field literally named
        # ``get`` — vanishingly unlikely, but Rule 7 prefers strict
        # over clever.
        if name == "get":
            return self._data.get
        v = self._data.get(name)
        if isinstance(v, dict):
            return _NS(v)
        return v

    def __contains__(self, key: object) -> bool:
        return key in self._data


def _build_functions(db: Database) -> Dict[str, Any]:
    """Whitelisted callables exposed to policy expressions."""
    funcs: Dict[str, Any] = {
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "abs": abs,
    }
    funcs.update(fw_builtins.STATELESS_BUILTINS)
    funcs.update(fw_builtins.build_history_builtins(db))
    return funcs


def _evaluate(policy: Policy, names: Dict[str, Any], funcs: Dict[str, Any]) -> bool:
    """Run the policy's condition. Returns True iff the rule fires.

    Empty / missing condition = always fires (lifecycle filtering already
    narrowed scope, so a policy without a condition is "block all
    matching this lifecycle/agent/tool").
    """
    cond = (policy.condition or "").strip()
    if not cond:
        return True
    # EvalWithCompoundTypes (vs plain SimpleEval) supports list / dict /
    # set literals + the `in` operator across them. Operators write
    # natural conditions like ``tool_name in ["shell", "exec",
    # "bash"]`` instead of OR-chains. Same security posture — only
    # the whitelisted ``functions`` set can be invoked, and the
    # validator's AST walker still rejects method calls except on
    # `_NS.get(...)` per PR #49.
    from simpleeval import EvalWithCompoundTypes
    e = EvalWithCompoundTypes(names=names, functions=funcs)
    result = e.eval(cond)
    return bool(result)


def _record_decision(
    db: Database,
    *,
    policy: Optional[Policy],
    lifecycle: str,
    decision: str,
    reason: str,
    trace_id: Optional[str],
    span_id: Optional[str],
    session_id: Optional[str],
    agent: Optional[str],
    project: Optional[str],
    tool_name: Optional[str],
    duration_ms: int,
    mode_at_decision: str,
) -> str:
    """Insert a row in `decisions`. Returns the decision_id even when
    insert fails — caller still wants something to put in the response.

    For block-class decisions (block / require_approval / rewrite) we
    *also* mirror to ``policy_violations`` so the legacy violations
    page (and ``trace.violation_count`` aggregate) surfaces firewall
    actions naturally. Without this bridge operators look at
    /violations to find "what did Lumin catch?" and see zeros while
    /decisions has rows — the two views had silently diverged.
    """
    decision_id = "dec_" + uuid.uuid4().hex[:24]
    policy_name = policy.name if policy else "_engine_"
    try:
        db.execute(
            """
            INSERT INTO decisions (
                id, policy_id, policy_name, lifecycle, decision,
                mode_at_decision, reason, trace_id, span_id,
                session_id, agent, project, tool_name,
                matched_field, matched_value_truncated,
                decision_at, duration_ms, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                decision_id,
                policy_name,
                policy_name,
                lifecycle,
                decision,
                mode_at_decision,
                reason[:500] if reason else None,
                trace_id, span_id, session_id, agent, project, tool_name,
                None, None,
                datetime.now(timezone.utc).replace(tzinfo=None),
                int(duration_ms),
                None,
            ],
        )
    except Exception:
        logger.exception("firewall.decide: failed to insert decision row")

    if decision in {"block", "require_approval", "rewrite"} and trace_id:
        _mirror_decision_as_violation(
            db,
            policy=policy,
            policy_name=policy_name,
            decision=decision,
            mode_at_decision=mode_at_decision,
            reason=reason,
            trace_id=trace_id,
            span_id=span_id,
        )
    return decision_id


# Severity defaults when the policy doesn't carry a severity field.
# Tuned so the dashboard's /violations page doesn't get drowned in
# 'critical' rows just because the firewall is in enforce mode.
_DECISION_SEVERITY_DEFAULTS = {
    "block": "high",
    "require_approval": "medium",
    "rewrite": "low",
}


def _mirror_decision_as_violation(
    db: Database,
    *,
    policy: Optional[Policy],
    policy_name: str,
    decision: str,
    mode_at_decision: str,
    reason: str,
    trace_id: str,
    span_id: Optional[str],
) -> None:
    """Mirror a firewall decision to the ``policy_violations`` table.

    Idempotent: id is derived from (policy_name, trace_id, span_id) so
    re-runs of the same decision context dedupe automatically (matches
    the SDK ingest endpoint's id scheme — both can write to the same
    row without conflict).
    """
    try:
        from policy_runtime import _violation_id  # local import: avoids circular deps
        vid = _violation_id(policy_name, trace_id, span_id)
        severity = (
            getattr(policy, "severity", None)
            if policy is not None
            else None
        ) or _DECISION_SEVERITY_DEFAULTS.get(decision, "medium")
        # action_taken includes the mode so the operator can spot
        # shadow-mode rows on the violations page (they look ghostly).
        action_taken = (
            f"{decision}:{mode_at_decision}"
            if mode_at_decision and mode_at_decision != "enforce"
            else decision
        )
        condition_text = (reason or "")[:500] or None
        db.execute(
            """
            INSERT INTO policy_violations (
                id, policy_name, policy_description, span_id, trace_id,
                condition_text, action_taken, severity, actual_value,
                webhook_fired, webhook_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                vid,
                policy_name,
                getattr(policy, "description", None) if policy else None,
                span_id,
                trace_id,
                condition_text,
                action_taken,
                severity,
                None,
                False,
                None,
            ],
        )
    except Exception:
        # Rule 7: never let a violations-mirror failure bubble up and
        # break the firewall response. Log and move on.
        logger.exception(
            "firewall.decide: failed to mirror decision %s/%s to policy_violations",
            policy_name,
            decision,
        )


def _create_approval(
    db: Database,
    *,
    decision_id: str,
    policy: Policy,
    trace_id: Optional[str],
    agent: Optional[str],
    tool_name: Optional[str],
    params: Optional[Dict[str, Any]],
) -> str:
    """Insert an approvals row in pending state. Caller surfaces the
    id back to the agent, which long-polls until state flips."""
    approval_id = "apv_" + uuid.uuid4().hex[:24]
    on_timeout = getattr(policy, "on_timeout", "allow") or "allow"
    timeout_s = 600
    from datetime import timedelta
    requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    timeout_at = requested_at + timedelta(seconds=timeout_s)
    try:
        # Truncate params payload — we don't want a massive blob in
        # the operator inbox view, and DuckDB JSON columns are happier
        # under 64KB.
        params_blob = _truncate_params(params)
        db.execute(
            """
            INSERT INTO approvals (
                id, decision_id, policy_id, trace_id, agent, tool_name,
                params_truncated, state, requested_at, resolved_at,
                resolved_by, resolution_reason, timeout_at, on_timeout
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL,
                      ?, ?)
            """,
            [
                approval_id, decision_id, policy.name, trace_id, agent,
                tool_name, params_blob, requested_at, timeout_at, on_timeout,
            ],
        )
    except Exception:
        logger.exception("firewall.decide: failed to insert approval row")
    return approval_id


def _truncate_params(params: Optional[Dict[str, Any]]) -> Optional[str]:
    if params is None:
        return None
    try:
        s = json.dumps(params)
    except Exception:
        s = str(params)
    return s[:8192]


def _redact_for_rewrite(
    params: Optional[Dict[str, Any]], output: Any
) -> Dict[str, Any]:
    """Default rewrite: redact PII/secrets from string fields. Cheap
    and conservative — operators who want richer rewrite logic can
    register custom builtins later."""
    out: Dict[str, Any] = {}
    if isinstance(params, dict):
        out["params"] = {
            k: fw_builtins.redact_pii(v) if isinstance(v, str) else v
            for k, v in params.items()
        } if params else {}
    if isinstance(output, str):
        out["result"] = fw_builtins.redact_pii(output)
    elif output is not None:
        out["result"] = output
    return out


def _allow_response(
    t0: float,
    *,
    reason: Optional[str] = None,
    shadow_hits: Optional[List[Dict[str, Any]]] = None,
    panic_reason: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "decision": "allow",
        "duration_ms": _elapsed_ms(t0),
    }
    if reason:
        body["reason"] = reason
    if shadow_hits:
        body["shadow_hits"] = shadow_hits
    if panic_reason:
        body["panic_reason"] = panic_reason
    return body


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
