"""LangChain integration for Synaptic.

Records every LLM call, tool call, and chain step as a Synaptic span.
Use explicitly:

    from synaptic.integrations.langchain import SynapticCallbackHandler
    handler = SynapticCallbackHandler()
    llm = ChatOpenAI(callbacks=[handler])

Or zero-config via env var (set BEFORE importing LangChain):

    import os
    os.environ["SYNAPTIC_TRACING"] = "true"
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

try:
    from langchain_core.callbacks.base import BaseCallbackHandler
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import LLMResult
    from langchain_core.tracers.context import register_configure_hook
except ImportError as e:
    raise ImportError(
        "synaptic.integrations.langchain requires langchain-core. "
        "Install with: pip install langchain-core"
    ) from e

from synaptic.sdk import _get_sdk
from synaptic.span import Span


# Approximate USD prices per 1K tokens (May 2026).
# Match by longest-prefix to handle versioned model names.
PRICES_PER_1K: Dict[str, tuple] = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.010),
    "gpt-4-turbo": (0.010, 0.030),
    "gpt-4": (0.030, 0.060),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-haiku-4": (0.001, 0.005),
}


def _compute_cost(model: Optional[str], tin: Optional[int], tout: Optional[int]) -> Optional[float]:
    if not model or tin is None or tout is None:
        return None
    m = model.lower()
    # Longest-prefix match
    best: Optional[tuple] = None
    best_key: str = ""
    for key, prices in PRICES_PER_1K.items():
        if m.startswith(key) and len(key) > len(best_key):
            best = prices
            best_key = key
    if best is None:
        return None
    inp, outp = best
    return round(tin * inp / 1000 + tout * outp / 1000, 8)


@dataclass
class _ExtSpan(Span):
    """Span subclass with the rich fields used by framework integrations.

    The SDK exporter calls ``.to_dict()`` on each span — overriding it here
    adds extra fields without modifying the SDK core dataclass. The API
    already accepts these fields (see packages/api/models.py:SpanInput).
    """

    model: Optional[str] = None
    provider: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    cost_usd: Optional[float] = None
    tool_name: Optional[str] = None

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["model"] = self.model
        d["provider"] = self.provider
        d["tokens_input"] = self.tokens_input
        d["tokens_output"] = self.tokens_output
        d["cost_usd"] = self.cost_usd
        d["tool_name"] = self.tool_name
        return d


def _serialize(value: Any, max_size: int = 10_240) -> Optional[str]:
    if value is None:
        return None
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        s = str(value)
    return s[:max_size]


def _msg_to_dict(m: BaseMessage) -> dict:
    return {
        "role": getattr(m, "type", "unknown"),
        "content": getattr(m, "content", ""),
    }


def _extract_name(serialized: Optional[dict]) -> Optional[str]:
    if not serialized:
        return None
    if "name" in serialized and serialized["name"]:
        return serialized["name"]
    id_path = serialized.get("id")
    if isinstance(id_path, list) and id_path:
        return id_path[-1]
    return None


def _extract_provider(serialized: Optional[dict]) -> Optional[str]:
    if not serialized:
        return None
    id_path = serialized.get("id") or []
    if isinstance(id_path, list):
        for known in ("openai", "anthropic", "google", "cohere", "mistral", "ollama"):
            if known in id_path:
                return known
    return None


def _extract_model(
    serialized: Optional[dict], metadata: Optional[dict] = None
) -> Optional[str]:
    candidates = []
    if serialized:
        kwargs = serialized.get("kwargs") or {}
        candidates.extend(
            [
                kwargs.get("model_name"),
                kwargs.get("model"),
                kwargs.get("deployment_name"),
            ]
        )
    if metadata:
        candidates.extend(
            [
                metadata.get("ls_model_name"),
                metadata.get("model"),
            ]
        )
    for c in candidates:
        if c:
            return c
    return None


def _extract_tokens(response: LLMResult) -> tuple[Optional[int], Optional[int]]:
    """Token usage location varies by provider — try the common ones."""
    llm_output = response.llm_output or {}

    # OpenAI: llm_output.token_usage.{prompt_tokens, completion_tokens}
    usage = llm_output.get("token_usage") or llm_output.get("usage")
    if isinstance(usage, dict):
        tin = usage.get("prompt_tokens") or usage.get("input_tokens")
        tout = usage.get("completion_tokens") or usage.get("output_tokens")
        if tin is not None or tout is not None:
            return tin, tout

    # Anthropic: usage_metadata on the message itself
    try:
        gen = response.generations[0][0]
        msg = getattr(gen, "message", None)
        if msg is not None:
            um = getattr(msg, "usage_metadata", None)
            if isinstance(um, dict):
                return um.get("input_tokens"), um.get("output_tokens")
    except (IndexError, AttributeError):
        pass

    return None, None


class SynapticCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that records spans into Synaptic.

    Lifecycle is keyed by LangChain's ``run_id`` (a UUID): we store an open
    span per active run, populate it with end/error data when the run
    completes, then submit it to the SDK queue.

    Parent-child nesting is established via LangChain's ``parent_run_id``
    — when a callback fires, we look up the parent span by run_id; if
    found, the new span gets ``parent_span_id = parent.id`` and inherits
    ``trace_id``. If the parent isn't tracked (e.g., handler was attached
    mid-chain), the new span becomes its own root.
    """

    def __init__(self) -> None:
        super().__init__()
        self._spans: Dict[UUID, _ExtSpan] = {}

    # --- helpers ---

    def _create_span(
        self,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        name: str,
        type: str,
        input_payload: Any,
    ) -> _ExtSpan:
        new_id = str(run_id)

        # Resolve parent: prefer LangChain's run-id chain (specific to this
        # callback handler), fall back to the SDK's current span (covers
        # frameworks like CrewAI that open Synaptic spans around LangChain
        # invocations) — only then become our own root.
        lc_parent = self._spans.get(parent_run_id) if parent_run_id else None
        if lc_parent is not None:
            trace_id = lc_parent.trace_id
            parent_span_id = lc_parent.id
        else:
            from synaptic.context import get_current_span

            outer = get_current_span()
            if outer is not None:
                trace_id = outer.trace_id
                parent_span_id = outer.id
            else:
                trace_id = new_id
                parent_span_id = None

        span = _ExtSpan(
            id=new_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            name=name,
            type=type,
        )
        if input_payload is not None:
            span.input = _serialize(input_payload)
        self._spans[run_id] = span
        return span

    def _finish(
        self,
        run_id: UUID,
        output_payload: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        span = self._spans.pop(run_id, None)
        if span is None:
            return
        if output_payload is not None and span.output is None:
            span.output = _serialize(output_payload)
        if error is not None:
            span.set_error(error)
        span.end()
        try:
            _get_sdk().submit(span)
        except Exception:
            pass

    # --- LLM (completion-style) ---

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
        span = self._create_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=_extract_name(serialized) or "llm",
            type="llm",
            input_payload={"prompts": prompts},
        )
        span.model = _extract_model(serialized, metadata)
        span.provider = _extract_provider(serialized)

    # --- Chat model (messages-style) ---

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        flat = messages[0] if messages else []
        span = self._create_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=_extract_name(serialized) or "chat",
            type="llm",
            input_payload={"messages": [_msg_to_dict(m) for m in flat]},
        )
        span.model = _extract_model(serialized, metadata)
        span.provider = _extract_provider(serialized)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        span = self._spans.get(run_id)
        if span is not None:
            # Output text
            try:
                gens = response.generations[0]
                text = getattr(gens[0], "text", "") if gens else ""
            except (IndexError, AttributeError):
                text = ""
            span.output = _serialize({"text": text})

            # Tokens
            tin, tout = _extract_tokens(response)
            span.tokens_input = tin
            span.tokens_output = tout

            # Model fallback from llm_output if not already set
            if not span.model:
                lo = response.llm_output or {}
                span.model = lo.get("model_name") or lo.get("model")

            span.cost_usd = _compute_cost(span.model, tin, tout)

        self._finish(run_id)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        self._finish(run_id, error=error)

    # --- Tool ---

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        name = _extract_name(serialized) or "tool"
        span = self._create_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=name,
            type="tool",
            input_payload=inputs if inputs is not None else {"input": input_str},
        )
        span.tool_name = name

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        self._finish(run_id, output_payload={"output": str(output)})

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        self._finish(run_id, error=error)

    # --- Chain ---

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        self._create_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            name=_extract_name(serialized) or "chain",
            type="custom",
            input_payload=inputs,
        )

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        self._finish(run_id, output_payload=outputs)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> Any:
        self._finish(run_id, error=error)


# --- Auto-registration via SYNAPTIC_TRACING env var ---
# When SYNAPTIC_TRACING=true|1|yes is set, LangChain's _configure() will
# instantiate SynapticCallbackHandler and attach it to every callback
# manager — covering all chains, LLMs, and tools without code changes.
_handler_var: ContextVar[Optional[SynapticCallbackHandler]] = ContextVar(
    "synaptic_handler", default=None
)
register_configure_hook(
    _handler_var,
    inheritable=True,
    handle_class=SynapticCallbackHandler,
    env_var="SYNAPTIC_TRACING",
)


__all__ = ["SynapticCallbackHandler"]
