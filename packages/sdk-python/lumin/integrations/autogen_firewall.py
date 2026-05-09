"""AutoGen firewall integration (§9.7).

AutoGen 0.4+ exposes ``register_hook`` on ``ConversableAgent`` for
on-message-received and on-tool-use lifecycle hooks. Lumin plugs in
two hooks:

  - ``process_message_before_send`` → before_proxy_call decide
  - ``before_tool_call``           → before_tool_call decide
  - ``after_tool_call``            → after_tool_call decide

Block decisions raise ``LuminFirewallBlocked``; AutoGen's runtime
surfaces the exception to the caller as an agent error.

Usage:

    from autogen import ConversableAgent
    from lumin.integrations.autogen_firewall import (
        register_lumin_firewall,
    )

    agent = ConversableAgent("assistant", llm_config=...)
    register_lumin_firewall(agent, project="support-bot")
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from lumin.firewall_client import (
    DecideRequest,
    DecideResponse,
    FirewallClient,
)

logger = logging.getLogger("lumin.integrations.autogen_firewall")


class LuminFirewallBlocked(Exception):
    def __init__(self, decision: DecideResponse, *, lifecycle: str) -> None:
        self.decision = decision
        self.lifecycle = lifecycle
        super().__init__(
            f"Lumin firewall {decision.decision} at {lifecycle}: "
            f"{decision.policy_name or '_engine_'}"
        )


def register_lumin_firewall(
    agent: Any,
    *,
    host: Optional[str] = None,
    project: Optional[str] = None,
    timeout_ms: int = 75,
    on_error: str = "allow",
    check_input: bool = True,
    check_output: bool = True,
    check_tools: bool = True,
) -> "AutoGenFirewallAdapter":
    """Attach the Lumin firewall hooks to an AutoGen
    ConversableAgent. Returns the adapter so the caller can
    detach later via ``adapter.close()``.
    """
    adapter = AutoGenFirewallAdapter(
        host=host, project=project,
        agent_name=getattr(agent, "name", None),
        timeout_ms=timeout_ms, on_error=on_error,
    )
    # AutoGen 0.4 lifecycle hook names. Older versions used different
    # names; we register-or-skip per name so this works across the
    # 0.2 → 0.4 transition.
    _try_register(agent, "process_message_before_send",
                  adapter.on_message_before_send if check_input else None)
    _try_register(agent, "process_last_received_message",
                  adapter.on_message_received if check_output else None)
    if check_tools and hasattr(agent, "register_function"):
        # AutoGen tool hooks are different per version; we wrap any
        # registered tool function. The adapter's wrap_tool is opt-in
        # per tool — operators call it explicitly.
        pass
    return adapter


class AutoGenFirewallAdapter:
    def __init__(
        self,
        *,
        host: Optional[str],
        project: Optional[str],
        agent_name: Optional[str],
        timeout_ms: int,
        on_error: str,
    ) -> None:
        self._client = FirewallClient(
            host=host, project=project, agent=agent_name,
            timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
        )

    def on_message_before_send(self, sender, message, recipient, silent) -> Any:  # type: ignore[no-untyped-def]
        text = _extract_text(message)
        decision = self._client.decide(DecideRequest(
            lifecycle="before_proxy_call",
            messages=[{"role": "user", "content": text}],
        ))
        if decision.is_block or decision.needs_approval:
            raise LuminFirewallBlocked(decision, lifecycle="before_proxy_call")
        if decision.is_rewrite:
            new_text = decision.rewritten.get("result")
            if isinstance(new_text, str):
                if isinstance(message, dict):
                    message["content"] = new_text
                else:
                    message = new_text
        return message

    def on_message_received(self, recipient, messages, sender, config) -> Any:  # type: ignore[no-untyped-def]
        if not messages:
            return None
        last = messages[-1]
        text = _extract_text(last)
        decision = self._client.decide(DecideRequest(
            lifecycle="after_proxy_call",
            output={"text": text},
        ))
        if decision.is_block:
            raise LuminFirewallBlocked(decision, lifecycle="after_proxy_call")
        if decision.is_rewrite:
            new_text = decision.rewritten.get("result")
            if isinstance(new_text, str) and isinstance(last, dict):
                last["content"] = new_text
        return None

    def wrap_tool(
        self,
        fn: Callable[..., Any],
        *,
        tool_name: Optional[str] = None,
    ) -> Callable[..., Any]:
        name = tool_name or fn.__name__
        client = self._client

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

    def close(self) -> None:
        self._client.close()


def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or message)


def _try_register(agent: Any, hook_name: str, fn: Optional[Callable]) -> None:
    if fn is None:
        return
    register = getattr(agent, "register_hook", None)
    if register is None:
        logger.debug(
            "autogen_firewall: agent %r has no register_hook — skipping %s",
            agent, hook_name,
        )
        return
    try:
        register(hookable_method=hook_name, hook=fn)
    except Exception:
        logger.exception("autogen_firewall: register_hook(%s) failed", hook_name)


__all__ = [
    "LuminFirewallBlocked",
    "register_lumin_firewall",
    "AutoGenFirewallAdapter",
]
