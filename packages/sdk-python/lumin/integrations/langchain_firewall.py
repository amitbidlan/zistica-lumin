"""LangChain firewall integration (§9.4).

Wraps Lumin's policy decide-engine into a LangChain BaseCallbackHandler.
Plug it into any LangChain agent/chain/tool to gate execution at:

  - on_tool_start  → before_tool_call decide. Block / require_approval
                     verbs raise ``LuminFirewallBlocked`` so the chain
                     halts before the tool fires. Rewrite verbs replace
                     the tool params.
  - on_tool_end    → after_tool_call decide. Block / rewrite verbs on
                     the tool's output let the firewall censor results
                     before they hit the model.
  - on_llm_start   → before_proxy_call decide on the user message
                     (skipped when ``proxied=True`` — the OpenAI-
                     compatible proxy at /v1/openai handles B/C
                     automatically and the callback would double-bill).
  - on_llm_end     → after_proxy_call decide on the model output.

Usage:

    from langchain.agents import AgentExecutor
    from lumin.integrations.langchain_firewall import LuminFirewallHandler

    fw = LuminFirewallHandler(project="support-bot", agent="cs_agent")
    agent = AgentExecutor.from_agent_and_tools(
        ..., callbacks=[fw],
    )

The observability handler in ``lumin.integrations.langchain`` is a
separate concern — both can be attached at once. Order is irrelevant;
LangChain dispatches callbacks in registration order but each one
operates on its own state.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

try:
    from langchain_core.callbacks.base import BaseCallbackHandler
    from langchain_core.outputs import LLMResult
except ImportError as e:  # pragma: no cover — surfaced at import time
    raise ImportError(
        "lumin.integrations.langchain_firewall requires langchain-core. "
        "Install with: pip install langchain-core"
    ) from e

from lumin.firewall_client import (
    DecideRequest,
    DecideResponse,
    FirewallClient,
)

logger = logging.getLogger("lumin.integrations.langchain_firewall")


class LuminFirewallBlocked(Exception):
    """Raised when a Lumin block / require_approval verb fires inside
    a LangChain tool/llm callback. Carries the full decision so the
    caller (often an AgentExecutor) can render a useful error."""

    def __init__(self, decision: DecideResponse, *, lifecycle: str) -> None:
        self.decision = decision
        self.lifecycle = lifecycle
        super().__init__(
            f"Lumin firewall {decision.decision} at {lifecycle}: "
            f"{decision.policy_name or '_engine_'} — "
            f"{decision.reason or 'no reason'}"
        )


class LuminFirewallHandler(BaseCallbackHandler):
    """LangChain callback handler that calls Lumin's firewall on
    every tool invocation and (optionally) every LLM call."""

    raise_error = True  # propagate LuminFirewallBlocked up the chain

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        timeout_ms: int = 75,
        on_error: str = "allow",
        proxied: bool = False,
        check_llm: bool = True,
    ) -> None:
        super().__init__()
        self._client = FirewallClient(
            host=host, project=project, agent=agent,
            timeout_ms=timeout_ms, on_error=on_error,  # type: ignore[arg-type]
        )
        self.proxied = proxied
        self.check_llm = check_llm
        # run_id → tool_name + params so on_tool_end can re-decide
        # against the same tool context.
        self._tool_runs: Dict[str, Dict[str, Any]] = {}
        # run_id → input messages so on_llm_end can include the
        # original user message in the after_proxy_call decide.
        self._llm_runs: Dict[str, List[Dict[str, Any]]] = {}

    # ----- tool gating -----

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        tool_name = (serialized or {}).get("name") or "unknown_tool"
        params = inputs if inputs is not None else {"input": input_str}
        self._tool_runs[str(run_id)] = {"tool_name": tool_name, "params": params}

        decision = self._client.decide(DecideRequest(
            lifecycle="before_tool_call",
            tool_name=tool_name,
            params=params,
            session_id=_session_from_metadata(metadata),
            trace_id=str(parent_run_id) if parent_run_id else None,
            span_id=str(run_id),
        ))
        self._enforce(decision, lifecycle="before_tool_call")

        # Rewrite: substitute params before the tool runs. Returning
        # a value from on_tool_start doesn't influence LangChain's
        # actual tool inputs (the API doesn't expose that surface),
        # so we mutate ``inputs`` in-place when the framework gives
        # us a dict reference.
        if decision.is_rewrite and inputs is not None:
            new_params = decision.rewritten.get("params") or {}
            for k, v in new_params.items():
                inputs[k] = v

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        ctx = self._tool_runs.pop(str(run_id), {})
        decision = self._client.decide(DecideRequest(
            lifecycle="after_tool_call",
            tool_name=ctx.get("tool_name"),
            params=ctx.get("params"),
            output={"result": output},
            trace_id=str(parent_run_id) if parent_run_id else None,
            span_id=str(run_id),
        ))
        self._enforce(decision, lifecycle="after_tool_call")

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        # Errors don't go through the firewall — clean up state.
        self._tool_runs.pop(str(run_id), None)

    # ----- LLM gating -----

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        if not self.check_llm or self.proxied:
            return
        # Keep the prompts so on_llm_end has them for after_proxy_call.
        msgs = [{"role": "user", "content": p} for p in prompts]
        self._llm_runs[str(run_id)] = msgs

        decision = self._client.decide(DecideRequest(
            lifecycle="before_proxy_call",
            messages=msgs,
            session_id=_session_from_metadata(metadata),
            trace_id=str(parent_run_id) if parent_run_id else None,
            span_id=str(run_id),
        ))
        self._enforce(decision, lifecycle="before_proxy_call")

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        if not self.check_llm or self.proxied:
            return
        msgs = self._llm_runs.pop(str(run_id), None)
        # Concatenate generation texts as the model's reply payload.
        try:
            text = "\n".join(
                gen.text for sub in response.generations for gen in sub
            )
        except Exception:
            text = ""
        decision = self._client.decide(DecideRequest(
            lifecycle="after_proxy_call",
            messages=msgs,
            output={"text": text},
            trace_id=str(parent_run_id) if parent_run_id else None,
            span_id=str(run_id),
        ))
        self._enforce(decision, lifecycle="after_proxy_call")

    # ----- internals -----

    def _enforce(self, decision: DecideResponse, *, lifecycle: str) -> None:
        if decision.is_block or decision.needs_approval:
            raise LuminFirewallBlocked(decision, lifecycle=lifecycle)

    def close(self) -> None:
        self._client.close()


def _session_from_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    if not metadata:
        return None
    for k in ("session_id", "thread_id", "conversation_id"):
        v = metadata.get(k)
        if isinstance(v, str) and v:
            return v
    return None


__all__ = ["LuminFirewallHandler", "LuminFirewallBlocked"]
