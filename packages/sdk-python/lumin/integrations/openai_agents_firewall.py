"""OpenAI Agents SDK firewall integration (§9.6).

The OpenAI Agents SDK exposes an ``input_guardrails`` /
``output_guardrails`` field on ``Agent`` plus a ``handoffs`` /
``tools`` runtime that fires lifecycle hooks. Lumin plugs into:

  - ``input_guardrails``  → before_proxy_call decide on user input
  - ``output_guardrails`` → after_proxy_call decide on agent output
  - tool wrappers        → before_tool_call / after_tool_call
                            sandwich around any tool the agent calls

Two adapter shapes:

  1. ``LuminInputGuardrail`` / ``LuminOutputGuardrail`` —
     instantiate and pass into ``Agent(input_guardrails=[...])``.

  2. ``wrap_tool(tool)`` — for tools defined as plain functions
     under ``@function_tool``; wrapping returns a new function that
     calls decide() before invoking.

The SDK was renamed several times across versions (``agents``,
``openai-agents``); the import is wrapped so this module loads even
if the SDK isn't installed — the guardrail classes still work for
sync ``check_input()`` / ``check_output()`` calls in standalone code.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from lumin.firewall_client import (
    DecideRequest,
    DecideResponse,
    FirewallClient,
)

logger = logging.getLogger("lumin.integrations.openai_agents_firewall")


class LuminFirewallBlocked(Exception):
    def __init__(self, decision: DecideResponse, *, lifecycle: str) -> None:
        self.decision = decision
        self.lifecycle = lifecycle
        super().__init__(
            f"Lumin firewall {decision.decision} at {lifecycle}: "
            f"{decision.policy_name or '_engine_'}"
        )


class _BaseGuardrail:
    def __init__(
        self,
        *,
        host: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        timeout_ms: int = 75,
        on_error: str = "allow",
    ) -> None:
        self._client = FirewallClient(
            host=host, project=project, agent=agent,
            timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        self._client.close()


class LuminInputGuardrail(_BaseGuardrail):
    """Use as ``Agent(input_guardrails=[LuminInputGuardrail()])``."""

    def check_input(
        self,
        *,
        messages: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        decision = self._client.decide(DecideRequest(
            lifecycle="before_proxy_call",
            messages=messages,
            session_id=session_id,
            model=model,
        ))
        if decision.is_block or decision.needs_approval:
            raise LuminFirewallBlocked(decision, lifecycle="before_proxy_call")

    # OpenAI Agents SDK calls ``__call__(context, agent_output) -> GuardrailFunctionOutput``
    async def __call__(self, context, agent_output) -> Any:  # type: ignore[no-untyped-def]
        text = _extract_input_text(context)
        try:
            self.check_input(messages=[{"role": "user", "content": text}])
        except LuminFirewallBlocked as blocked:
            return _guardrail_failure_output(
                tripwire=True, output=str(blocked.decision.reason or "blocked"),
            )
        return _guardrail_failure_output(tripwire=False, output="")


class LuminOutputGuardrail(_BaseGuardrail):
    """Use as ``Agent(output_guardrails=[LuminOutputGuardrail()])``."""

    def check_output(
        self,
        *,
        messages: List[Dict[str, Any]],
        text: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        decision = self._client.decide(DecideRequest(
            lifecycle="after_proxy_call",
            messages=messages,
            output={"text": text},
            session_id=session_id,
            model=model,
        ))
        if decision.is_block:
            raise LuminFirewallBlocked(decision, lifecycle="after_proxy_call")
        if decision.is_rewrite:
            new_text = decision.rewritten.get("result")
            if isinstance(new_text, str):
                return new_text
        return text

    async def __call__(self, context, agent_output) -> Any:  # type: ignore[no-untyped-def]
        text = _extract_output_text(agent_output)
        try:
            self.check_output(messages=[], text=text)
        except LuminFirewallBlocked as blocked:
            return _guardrail_failure_output(
                tripwire=True, output=str(blocked.decision.reason or "blocked"),
            )
        return _guardrail_failure_output(tripwire=False, output="")


def wrap_tool(
    fn: Callable[..., Any],
    *,
    tool_name: Optional[str] = None,
    host: Optional[str] = None,
    project: Optional[str] = None,
    agent: Optional[str] = None,
    timeout_ms: int = 75,
    on_error: str = "allow",
) -> Callable[..., Any]:
    """Wrap a tool function with before/after firewall gating.

    Use BEFORE the @function_tool decorator so the firewall checks
    happen inside the tool's call boundary."""
    client = FirewallClient(
        host=host, project=project, agent=agent,
        timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
    )
    name = tool_name or fn.__name__

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        before = client.decide(DecideRequest(
            lifecycle="before_tool_call",
            tool_name=name,
            params=kwargs,
        ))
        if before.is_block:
            raise LuminFirewallBlocked(before, lifecycle="before_tool_call")
        if before.is_rewrite:
            new_params = before.rewritten.get("params") or {}
            kwargs = {**kwargs, **new_params}

        result = fn(*args, **kwargs)

        after = client.decide(DecideRequest(
            lifecycle="after_tool_call",
            tool_name=name,
            output={"result": result},
        ))
        if after.is_block:
            raise LuminFirewallBlocked(after, lifecycle="after_tool_call")
        if after.is_rewrite:
            return after.rewritten.get("result", result)
        return result

    wrapped.__name__ = name
    wrapped.__doc__ = fn.__doc__
    return wrapped


# ----- helpers -----


def _extract_input_text(context: Any) -> str:
    try:
        msgs = getattr(context, "messages", None) or context.get("messages") or []
        for m in msgs:
            if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "user":
                return m["content"] if isinstance(m, dict) else getattr(m, "content", "")
    except Exception:
        pass
    return ""


def _extract_output_text(agent_output: Any) -> str:
    try:
        return getattr(agent_output, "final_output", None) or str(agent_output)
    except Exception:
        return ""


def _guardrail_failure_output(*, tripwire: bool, output: str) -> Any:
    """Build the GuardrailFunctionOutput shape the SDK expects.
    When the SDK isn't installed, return a plain dict with the
    same fields so test code can introspect."""
    try:
        from agents import GuardrailFunctionOutput  # type: ignore
        return GuardrailFunctionOutput(
            output_info={"reason": output}, tripwire_triggered=tripwire,
        )
    except Exception:
        return {"output_info": {"reason": output}, "tripwire_triggered": tripwire}


__all__ = [
    "LuminFirewallBlocked",
    "LuminInputGuardrail",
    "LuminOutputGuardrail",
    "wrap_tool",
]
