"""Pydantic AI firewall integration (§9.8).

Pydantic AI exposes typed tool functions via ``@agent.tool`` and
result validation via ``RunContext``. Lumin's adapter wraps tools
with before / after firewall checks and adds an input-side
``check_user_input`` helper for the message boundary.

Two adapter shapes:

  1. ``firewall_tool(agent, fn)`` — replaces ``@agent.tool``,
     registering ``fn`` with the Lumin firewall pre/post.

  2. ``LuminFirewallChecker`` — manual sync class with
     ``check_input()`` / ``check_output()`` / ``wrap_tool()`` for
     operators who don't want the decorator-replacement style.

Usage with the decorator:

    from pydantic_ai import Agent
    from lumin.integrations.pydantic_ai_firewall import firewall_tool

    agent = Agent(model="claude-3.5-sonnet", system_prompt="...")

    @firewall_tool(agent)
    def search_db(ctx, query: str) -> list:
        ...

The ``@firewall_tool`` decorator wraps the function with
before_tool_call / after_tool_call decide() calls, then registers
the wrapped function with the agent via the standard
``agent.tool`` decorator path.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, List, Optional

from lumin.firewall_client import (
    DecideRequest,
    DecideResponse,
    FirewallClient,
)

logger = logging.getLogger("lumin.integrations.pydantic_ai_firewall")


class LuminFirewallBlocked(Exception):
    def __init__(self, decision: DecideResponse, *, lifecycle: str) -> None:
        self.decision = decision
        self.lifecycle = lifecycle
        super().__init__(
            f"Lumin firewall {decision.decision} at {lifecycle}: "
            f"{decision.policy_name or '_engine_'}"
        )


class LuminFirewallChecker:
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

    def check_input(self, text: str, *, session_id: Optional[str] = None) -> None:
        decision = self._client.decide(DecideRequest(
            lifecycle="before_proxy_call",
            messages=[{"role": "user", "content": text}],
            session_id=session_id,
        ))
        if decision.is_block or decision.needs_approval:
            raise LuminFirewallBlocked(decision, lifecycle="before_proxy_call")

    def check_output(self, text: str, *, session_id: Optional[str] = None) -> str:
        decision = self._client.decide(DecideRequest(
            lifecycle="after_proxy_call",
            output={"text": text},
            session_id=session_id,
        ))
        if decision.is_block:
            raise LuminFirewallBlocked(decision, lifecycle="after_proxy_call")
        if decision.is_rewrite:
            new = decision.rewritten.get("result")
            if isinstance(new, str):
                return new
        return text

    def wrap_tool(
        self,
        fn: Callable[..., Any],
        *,
        tool_name: Optional[str] = None,
    ) -> Callable[..., Any]:
        name = tool_name or fn.__name__
        client = self._client

        @wraps(fn)
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

        return wrapped

    def close(self) -> None:
        self._client.close()


def firewall_tool(
    agent: Any,
    *,
    host: Optional[str] = None,
    project: Optional[str] = None,
    timeout_ms: int = 75,
    on_error: str = "allow",
    tool_name: Optional[str] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator-replacement for ``@agent.tool``. Wraps the tool
    with firewall pre/post checks and registers it with the agent.

    Falls back gracefully when the agent doesn't expose ``.tool``
    (e.g., a stub during testing) — the wrapped function is still
    returned and can be called directly.
    """
    checker = LuminFirewallChecker(
        host=host, project=project,
        agent=getattr(agent, "name", None) or None,
        timeout_ms=timeout_ms, on_error=on_error,
    )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        wrapped = checker.wrap_tool(fn, tool_name=tool_name or fn.__name__)
        register = getattr(agent, "tool", None)
        if callable(register):
            try:
                register(wrapped)
            except Exception:
                logger.exception(
                    "pydantic_ai_firewall: agent.tool registration failed; "
                    "returning wrapped function only"
                )
        return wrapped

    return decorator


__all__ = [
    "LuminFirewallBlocked",
    "LuminFirewallChecker",
    "firewall_tool",
]
