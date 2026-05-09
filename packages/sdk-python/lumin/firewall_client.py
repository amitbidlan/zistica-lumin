"""Synchronous client for ``POST /v1/policy/decide`` (§5.1).

Every framework integration in ``lumin.integrations.*_firewall`` calls
this. Centralising the HTTP / timeout / fail-mode / response-shape
logic in one place means a fix in any of those three lands in
every integration at once, and the per-framework adapter is a thin
mapping from framework hook → ``decide()`` → framework response shape.

Behavior contract (Rule 7):

  - The agent NEVER blocks waiting on us. Default timeout is 75ms;
    operators tighten or loosen via ``timeout_ms`` constructor arg.
  - On timeout / network error / 5xx, the configured ``on_error``
    mode determines whether the framework continues (``allow``,
    default) or stops (``deny``). Most production deployments use
    ``allow`` because a Lumin outage must not take the agent down.
  - The full response is exposed so framework-specific adapters can
    pick the bits they need (some only care about ``decision``,
    others want ``rewritten`` for output mutation).

Sync only — every supported framework's hook surface is
synchronous-friendly. An async variant lives behind ``adecide`` for
LangGraph / async OpenAI Agents pipelines.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

import httpx

logger = logging.getLogger("lumin.firewall_client")


DEFAULT_HOST = "http://localhost:8000"
DEFAULT_TIMEOUT_MS = 75
VALID_LIFECYCLES = (
    "before_proxy_call",
    "after_proxy_call",
    "before_tool_call",
    "after_tool_call",
    "post_ingest",
)


@dataclass
class DecideRequest:
    lifecycle: str
    tool_name: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    project: Optional[str] = None
    model: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    output: Any = None

    def to_payload(self) -> Dict[str, Any]:
        """Drop None values so the API sees a tight payload."""
        out = {
            "lifecycle": self.lifecycle,
            "tool_name": self.tool_name,
            "params": self.params,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "project": self.project,
            "model": self.model,
            "messages": self.messages,
            "output": self.output,
        }
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class DecideResponse:
    decision: str
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    reason: Optional[str] = None
    decision_id: Optional[str] = None
    mode_at_decision: Optional[str] = None
    duration_ms: Optional[int] = None
    approval_id: Optional[str] = None
    timeout_s: Optional[int] = None
    rewritten: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_allow(self) -> bool:
        return self.decision == "allow"

    @property
    def is_block(self) -> bool:
        return self.decision == "block"

    @property
    def needs_approval(self) -> bool:
        return self.decision == "require_approval"

    @property
    def is_rewrite(self) -> bool:
        return self.decision == "rewrite"

    @classmethod
    def allow(cls, *, reason: str = "", duration_ms: int = 0) -> "DecideResponse":
        return cls(decision="allow", reason=reason, duration_ms=duration_ms)


class FirewallClient:
    """Thin sync HTTP client for /v1/policy/decide.

    One instance per framework adapter. Reuses a single httpx.Client
    so connection pooling kicks in across many decide() calls in the
    same process — saves ~5–10ms per call vs. per-request clients.
    """

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        on_error: Literal["allow", "deny"] = "allow",
    ) -> None:
        self.host = (host or os.environ.get("LUMIN_HOST") or DEFAULT_HOST).rstrip("/")
        self.project = project or os.environ.get("LUMIN_PROJECT")
        self.agent = agent
        self.timeout_s = max(0.01, timeout_ms / 1000.0)
        self.on_error = on_error
        self._failure_logged = False
        self._client = httpx.Client(timeout=self.timeout_s)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "FirewallClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def decide(self, req: DecideRequest) -> DecideResponse:
        """Issue a synchronous decide call. Always returns a
        DecideResponse — even on timeout / network failure / 5xx.

        On error: the response carries decision=allow (or block,
        if on_error='deny') with reason='internal_error' so the
        caller can branch on it explicitly.
        """
        # Per-call lifecycle validation. Lumin's API also validates,
        # but doing it client-side surfaces typos at integration-test
        # time instead of inside a /v1/policy/decide 4xx response.
        if req.lifecycle not in VALID_LIFECYCLES:
            raise ValueError(
                f"lifecycle must be one of {VALID_LIFECYCLES}, got {req.lifecycle!r}"
            )

        # Operator can tag every decide call with project + agent
        # without setting them on every request.
        payload = req.to_payload()
        payload.setdefault("project", self.project)
        payload.setdefault("agent", self.agent)

        url = f"{self.host}/v1/policy/decide"
        started = time.monotonic()
        try:
            resp = self._client.post(url, json=payload)
            if resp.status_code != 200:
                return self._error_response(
                    f"http_{resp.status_code}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            data = resp.json() if resp.content else {}
        except httpx.TimeoutException:
            return self._error_response(
                "timeout",
                duration_ms=int(self.timeout_s * 1000),
            )
        except Exception as e:
            if not self._failure_logged:
                logger.warning("firewall_client: %s — further failures suppressed", e)
                self._failure_logged = True
            return self._error_response("network_error", duration_ms=0)

        return DecideResponse(
            decision=str(data.get("decision") or "allow"),
            policy_id=data.get("policy_id"),
            policy_name=data.get("policy_name"),
            reason=data.get("reason"),
            decision_id=data.get("decision_id"),
            mode_at_decision=data.get("mode_at_decision"),
            duration_ms=data.get("duration_ms"),
            approval_id=data.get("approval_id"),
            timeout_s=data.get("timeout_s"),
            rewritten=data.get("rewritten") or {},
        )

    def _error_response(self, reason: str, *, duration_ms: int) -> DecideResponse:
        verdict = "block" if self.on_error == "deny" else "allow"
        return DecideResponse(
            decision=verdict,
            reason=f"firewall_client:{reason}",
            duration_ms=duration_ms,
        )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_TIMEOUT_MS",
    "VALID_LIFECYCLES",
    "DecideRequest",
    "DecideResponse",
    "FirewallClient",
]
