"""Glue between SDK span/trace lifecycle and the PolicyEngine.

Owns:
  - the per-trace aggregator (running totals of cost, tokens, span count,
    error count) so trace_end conditions like
    ``trace.total_cost_usd > 0.10`` see the true total when the root
    span ends
  - violation transport — POST /v1/violations and optional webhook fire
  - the trigger logic — span_end runs on every span; trace_end runs
    once when a root span (parent_span_id is None) ends

Per Rule 7 every method here is best-effort. Errors are logged at
warning level and swallowed; the agent never sees them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .policy import PolicyEngine, PolicyViolation

logger = logging.getLogger("lumin.policy")


@dataclass
class _TraceAgg:
    """Running totals for a single trace, accumulated as spans land."""

    trace_id: str
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    span_count: int = 0
    error_count: int = 0
    first_started_at: Optional[str] = None
    last_ended_at: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    name: Optional[str] = None
    input: Optional[str] = None
    output: Optional[str] = None

    def add_span(self, span: Any) -> None:
        self.span_count += 1

        cost = getattr(span, "cost_usd", None)
        if cost is not None:
            try:
                self.total_cost_usd += float(cost)
            except (TypeError, ValueError):
                pass

        ti = getattr(span, "tokens_input", None) or 0
        to = getattr(span, "tokens_output", None) or 0
        try:
            self.total_tokens += int(ti) + int(to)
        except (TypeError, ValueError):
            pass

        if getattr(span, "error", None):
            self.error_count += 1

        started = getattr(span, "started_at", None)
        ended = getattr(span, "ended_at", None)
        if started and (self.first_started_at is None or started < self.first_started_at):
            self.first_started_at = started
        if ended and (self.last_ended_at is None or ended > self.last_ended_at):
            self.last_ended_at = ended

        sid = getattr(span, "session_id", None)
        if sid and self.session_id is None:
            self.session_id = sid

    def to_trace_dict(self, root_span: Any) -> dict:
        return {
            "id": self.trace_id,
            "trace_id": self.trace_id,
            "name": getattr(root_span, "name", None) or self.name,
            "input": getattr(root_span, "input", None),
            "output": getattr(root_span, "output", None),
            "total_cost_usd": self.total_cost_usd,
            "total_tokens": self.total_tokens,
            "span_count": self.span_count,
            "error_count": self.error_count,
            "started_at": self.first_started_at,
            "ended_at": self.last_ended_at,
            "session_id": self.session_id or getattr(root_span, "session_id", None),
        }


class PolicyDispatcher:
    """Per-SDK-instance helper. Owns the engine, the aggregator, and
    the async transport for violations + webhooks.

    All public methods are safe to call from any thread; async work
    is always scheduled onto the SDK's background loop, never run
    inline on the agent thread.
    """

    def __init__(
        self,
        engine: PolicyEngine,
        host: str,
        api_key: Optional[str] = None,
        alert_webhook: Optional[str] = None,
        timeout: float = 5.0,
    ):
        self._engine = engine
        self._violations_url = host.rstrip("/") + "/v1/violations"
        self._dashboard_base = host.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key
        self._timeout = timeout
        self._alert_webhook = alert_webhook
        self._client: Optional[httpx.AsyncClient] = None
        # trace_id → _TraceAgg. Populated as non-root spans land,
        # consumed + dropped when the root span ends.
        self._traces: Dict[str, _TraceAgg] = {}

    @property
    def engine(self) -> PolicyEngine:
        return self._engine

    # --- sync entry points called from the agent thread ---------------------

    def on_span_end(self, span: Any) -> List[PolicyViolation]:
        """Called by SDK.submit(span). Runs span_end + trace_end
        evaluations synchronously (microsecond cost via simpleeval)
        and returns the accumulated violations.

        Caller is responsible for shipping the returned violations to
        the API + firing webhooks via ``ship_async``.
        """
        if not self._engine:
            return []

        violations: List[PolicyViolation] = []

        try:
            # Always update the trace aggregator first so trace_end
            # evaluation (if this span is the root) sees fully-summed
            # totals.
            trace_id = getattr(span, "trace_id", None)
            if trace_id:
                agg = self._traces.get(trace_id)
                if agg is None:
                    agg = _TraceAgg(trace_id=trace_id)
                    self._traces[trace_id] = agg
                agg.add_span(span)
        except Exception:
            logger.exception("policy: failed to update trace aggregator")

        try:
            violations.extend(self._engine.evaluate_span(span))
        except Exception:
            logger.exception("policy: span evaluation crashed")

        # Root span → fire trace_end policies + drop the aggregator
        try:
            if getattr(span, "parent_span_id", None) is None and trace_id:
                agg = self._traces.pop(trace_id, None)
                if agg is not None:
                    trace_dict = agg.to_trace_dict(span)
                    violations.extend(self._engine.evaluate_trace(trace_dict))
        except Exception:
            logger.exception("policy: trace_end evaluation crashed")

        return violations

    # --- async transport ----------------------------------------------------

    async def ship_async(self, violations: List[PolicyViolation]) -> None:
        """POST violations to the API and fire any webhooks. Called
        from the SDK's background loop. Failures are swallowed."""
        if not violations:
            return

        # 1) POST to the API. One request per call — keeps the wire
        # format simple and matches the SpanInput shape we use for
        # /v1/spans.
        try:
            await self._post_violations(violations)
        except Exception:
            logger.exception("policy: violation ingest failed")

        # 2) Fire webhooks for each "alert" action
        for v in violations:
            try:
                await self._maybe_fire_webhook(v)
            except Exception:
                logger.exception(
                    "policy: webhook fire failed for %r", v.policy_name
                )

    async def _post_violations(self, violations: List[PolicyViolation]) -> None:
        self._ensure_client()
        payload = {"violations": [v.to_dict() for v in violations]}
        try:
            async with asyncio.timeout(self._timeout):
                assert self._client is not None
                await self._client.post(
                    self._violations_url, json=payload, headers=self._headers
                )
        except Exception:
            # Rule 7: agent must never fail because of Lumin
            pass

    async def _maybe_fire_webhook(self, v: PolicyViolation) -> None:
        if v.action_taken != "alert":
            return
        url = v.webhook_url or self._alert_webhook
        if not url:
            return

        body = {
            "type": "lumin_policy_violation",
            "policy_name": v.policy_name,
            "severity": v.severity,
            "trace_id": v.trace_id,
            "span_id": v.span_id,
            "condition": v.condition_text,
            "actual_value": v.actual_value,
            "dashboard_url": f"{self._dashboard_base.replace(':8000', ':3000')}/traces/{v.trace_id}",
            "timestamp": v.created_at,
        }
        self._ensure_client()
        try:
            async with asyncio.timeout(self._timeout):
                assert self._client is not None
                await self._client.post(url, json=body)
        except Exception:
            # Webhook failures must never reach the agent.
            logger.warning("policy: webhook to %s failed (swallowed)", url)

    def _ensure_client(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
