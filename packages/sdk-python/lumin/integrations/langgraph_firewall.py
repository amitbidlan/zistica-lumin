"""LangGraph firewall integration (§9.3).

LangGraph nodes don't speak the LangChain callback contract —
they're plain async / sync functions that read State and return a
partial State update. So the firewall plugs in via:

  - ``firewall_node(...)`` — a pre-built node that the operator
    installs at the entry of any sensitive subgraph. Calls
    before_proxy_call decide() against the current user message
    and either returns the State unchanged (allow) or interrupts
    via LangGraph's ``interrupt()`` primitive (block / approval).

  - ``wrap_tool_node(node, *, tool_name)`` — decorator that wraps
    an existing tool-call node so before_tool_call / after_tool_call
    decide() runs around it.

LangGraph provides ``interrupt()`` for human-in-the-loop semantics.
On block we return a Command that halts the graph; the dashboard's
ApprovalsInbox surfaces the pending decision to the operator who
can resolve it via /v1/approvals/{id}/resolve.

Usage:

    from langgraph.graph import StateGraph
    from lumin.integrations.langgraph_firewall import (
        firewall_node, wrap_tool_node,
    )

    builder = StateGraph(MyState)
    builder.add_node("firewall_input", firewall_node(project="bot"))
    builder.add_node(
        "search",
        wrap_tool_node(my_search_node, tool_name="web_search"),
    )
    builder.add_edge("firewall_input", "search")
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from lumin.firewall_client import (
    DecideRequest,
    DecideResponse,
    FirewallClient,
)

logger = logging.getLogger("lumin.integrations.langgraph_firewall")

try:
    from langgraph.types import interrupt as _interrupt
except ImportError:  # pragma: no cover
    _interrupt = None  # surfaced at runtime


class LuminFirewallBlocked(Exception):
    """Raised when a LangGraph node hits a block decision."""

    def __init__(self, decision: DecideResponse, *, lifecycle: str) -> None:
        self.decision = decision
        self.lifecycle = lifecycle
        super().__init__(
            f"Lumin firewall {decision.decision} at {lifecycle}: "
            f"{decision.policy_name or '_engine_'}"
        )


def firewall_node(
    *,
    host: Optional[str] = None,
    project: Optional[str] = None,
    agent: Optional[str] = None,
    timeout_ms: int = 75,
    on_error: str = "allow",
    user_message_key: str = "messages",
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build a LangGraph node that gates the user message at graph
    entry. Returns the state unchanged on allow, raises
    ``LuminFirewallBlocked`` on block, or invokes ``interrupt()``
    on require_approval (so the graph pauses for operator review).
    """
    client = FirewallClient(
        host=host, project=project, agent=agent,
        timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
    )

    def node(state: Dict[str, Any]) -> Dict[str, Any]:
        msgs = state.get(user_message_key) or []
        # LangGraph state usually has messages as a list of dicts or
        # langchain BaseMessage objects. Coerce to the JSON shape
        # decide() expects.
        normalized = [_normalize_message(m) for m in msgs if m is not None]

        decision = client.decide(DecideRequest(
            lifecycle="before_proxy_call",
            messages=normalized,
            session_id=str(state.get("thread_id") or "") or None,
        ))

        if decision.is_block:
            raise LuminFirewallBlocked(decision, lifecycle="before_proxy_call")
        if decision.needs_approval:
            if _interrupt is not None:
                _interrupt({
                    "kind": "lumin_firewall_approval",
                    "decision_id": decision.decision_id,
                    "approval_id": decision.approval_id,
                    "policy_name": decision.policy_name,
                    "reason": decision.reason,
                })
            else:
                raise LuminFirewallBlocked(
                    decision, lifecycle="before_proxy_call_approval"
                )
        return state

    node.__name__ = "lumin_firewall_input"
    return node


def wrap_tool_node(
    inner: Callable[[Dict[str, Any]], Any],
    *,
    tool_name: str,
    host: Optional[str] = None,
    project: Optional[str] = None,
    agent: Optional[str] = None,
    timeout_ms: int = 75,
    on_error: str = "allow",
    params_key: str = "tool_input",
    output_key: str = "tool_output",
) -> Callable[[Dict[str, Any]], Any]:
    """Wrap a LangGraph tool node with before_tool_call /
    after_tool_call gating. ``inner`` is the operator's existing
    node; we sandwich decide() calls around it."""
    client = FirewallClient(
        host=host, project=project, agent=agent,
        timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
    )

    def wrapped(state: Dict[str, Any]) -> Any:
        params = state.get(params_key) or {}
        before = client.decide(DecideRequest(
            lifecycle="before_tool_call",
            tool_name=tool_name,
            params=params,
            session_id=str(state.get("thread_id") or "") or None,
        ))
        if before.is_block:
            raise LuminFirewallBlocked(before, lifecycle="before_tool_call")
        if before.is_rewrite:
            new_params = before.rewritten.get("params") or {}
            state = {**state, params_key: {**params, **new_params}}

        result = inner(state)

        # ``result`` from a LangGraph node is typically a partial
        # state dict. Pull out the tool's output for after_tool_call.
        output = None
        if isinstance(result, dict):
            output = result.get(output_key)

        after = client.decide(DecideRequest(
            lifecycle="after_tool_call",
            tool_name=tool_name,
            output={"result": output},
            session_id=str(state.get("thread_id") or "") or None,
        ))
        if after.is_block:
            raise LuminFirewallBlocked(after, lifecycle="after_tool_call")
        if after.is_rewrite and isinstance(result, dict):
            new_output = after.rewritten.get("result")
            result = {**result, output_key: new_output}
        return result

    wrapped.__name__ = f"lumin_wrapped_{tool_name}"
    return wrapped


def _normalize_message(m: Any) -> Dict[str, Any]:
    if isinstance(m, dict):
        return {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
    role = getattr(m, "type", None) or getattr(m, "role", None) or "user"
    content = getattr(m, "content", None) or str(m)
    return {"role": str(role), "content": str(content)}


__all__ = [
    "LuminFirewallBlocked",
    "firewall_node",
    "wrap_tool_node",
]
