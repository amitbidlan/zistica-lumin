"""LlamaIndex integration for Synaptic.

Records every LLM call, retrieval step, embedding computation, and
query as a Synaptic span. Use explicitly:

    from synaptic.integrations.llama_index import SynapticCallbackHandler
    from llama_index.core import Settings
    from llama_index.core.callbacks import CallbackManager

    handler = SynapticCallbackHandler()
    Settings.callback_manager = CallbackManager([handler])

Near-zero config: set the env var and import this module once. The
import triggers a one-time attempt to attach the handler to
``llama_index.core.Settings.callback_manager``. After that, no further
code changes are needed.

    import os
    os.environ["SYNAPTIC_TRACING"] = "true"
    import synaptic.integrations.llama_index  # noqa: F401  (registers handler)

Note: the env var alone is not sufficient — Python has no global
package-discovery hook. The integration module must be imported at
least once for auto-registration to fire.

The handler implements LlamaIndex's ``BaseCallbackHandler`` contract:
``start_trace`` / ``end_trace`` bracket each query, and
``on_event_start`` / ``on_event_end`` wrap each operation inside it.
LlamaIndex itself supplies ``parent_id`` on each event start, so
nested-span linkage falls out for free.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

try:
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
    from llama_index.core.callbacks.schema import CBEventType, EventPayload
except ImportError as e:
    raise ImportError(
        "synaptic.integrations.llama_index requires llama-index-core. "
        "Install with: pip install llama-index-core>=0.10.0"
    ) from e

from synaptic.context import get_current_span
from synaptic.integrations._ext_span import _ExtSpan, _estimate_tokens, _serialize
from synaptic.sdk import _get_sdk


# Approximate USD prices per 1K tokens (May 2026), shared with the
# LangChain integration's price table — but kept local so this module
# doesn't import from langchain.py (which would force the langchain
# extra to be installed even when only LlamaIndex is in use).
PRICES_PER_1K: Dict[str, tuple] = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.010),
    "gpt-4-turbo": (0.010, 0.030),
    "gpt-4": (0.030, 0.060),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-haiku-4": (0.001, 0.005),
    # Embeddings — output rate is 0 since embeddings have no
    # generated tokens. Cost computed from tokens_input alone.
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
    "text-embedding-ada-002": (0.0001, 0.0),
}


def register_model_price(
    model_prefix: str, input_per_1k: float, output_per_1k: float
) -> None:
    """Add or override a row in the price table at runtime. Useful
    for self-hosted models, custom deployments, or new models that
    haven't been added to the built-in table yet.

    Match is longest-prefix on lowercased model name, so adding
    e.g. ``register_model_price("gpt-5", 0.005, 0.015)`` covers
    every ``gpt-5*`` variant.
    """
    PRICES_PER_1K[model_prefix.lower()] = (input_per_1k, output_per_1k)


def _normalize_model_name(model: str) -> str:
    """Strip provider/fine-tune prefixes that real production models
    use. Examples:
      - ``ft:gpt-4o:my-org::abc123`` → ``gpt-4o``
      - ``openai/gpt-4o-mini`` → ``gpt-4o-mini``
      - ``models/gemini-pro`` → ``gemini-pro``
    """
    m = model.lower()
    if m.startswith("ft:"):
        # OpenAI fine-tune format: ft:<base>:<org>::<id>
        rest = m[3:]
        return rest.split(":", 1)[0] if ":" in rest else rest
    if "/" in m:
        return m.split("/", 1)[1]
    return m


def _compute_cost(
    model: Optional[str], tin: Optional[int], tout: Optional[int]
) -> Optional[float]:
    """Longest-prefix match into the price table. Handles fine-tuned
    model names (``ft:gpt-4o:...``) and provider-prefixed names
    (``openai/gpt-4o``) by normalizing first."""
    if not model or tin is None or tout is None:
        return None
    m = _normalize_model_name(model)
    best: Optional[tuple] = None
    best_key = ""
    for key, prices in PRICES_PER_1K.items():
        if m.startswith(key) and len(key) > len(best_key):
            best = prices
            best_key = key
    if best is None:
        return None
    inp, outp = best
    return round(tin * inp / 1000 + tout * outp / 1000, 8)


# --- CBEventType → Synaptic span (type, name) -----------------------

# Mapping is intentionally explicit — silently lumping unknown events
# into "custom" with their event-type as the name keeps the dashboard
# readable when LlamaIndex adds new event types in the future.
_TYPE_NAME_MAP: Dict[CBEventType, tuple] = {
    CBEventType.LLM: ("llm", "llm_call"),
    CBEventType.RETRIEVE: ("retrieval", "retrieve"),
    CBEventType.EMBEDDING: ("embedding", "embedding"),
    CBEventType.QUERY: ("custom", "query"),
    CBEventType.CHUNKING: ("custom", "chunking"),
    CBEventType.NODE_PARSING: ("custom", "node_parsing"),
    CBEventType.SYNTHESIZE: ("custom", "synthesize"),
    CBEventType.TEMPLATING: ("custom", "templating"),
    CBEventType.SUB_QUESTION: ("custom", "sub_question"),
    CBEventType.RERANKING: ("custom", "reranking"),
    CBEventType.AGENT_STEP: ("custom", "agent_step"),
    CBEventType.FUNCTION_CALL: ("tool", "function_call"),
    CBEventType.TREE: ("custom", "tree"),
    CBEventType.EXCEPTION: ("custom", "exception"),
}


def _classify(event_type: CBEventType) -> tuple:
    return _TYPE_NAME_MAP.get(event_type, ("custom", event_type.value))


# --- Payload extraction --------------------------------------------


def _extract_text(value: Any) -> str:
    """Best-effort string conversion for diverse llama-index payloads.
    Handles raw strings, ChatMessage / ChatResponse objects, lists of
    either, dicts with role+content, and anything with a ``content``
    or ``.message.content`` attribute."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_extract_text(v) for v in value if v is not None)
    if isinstance(value, dict):
        # {"role": "user", "content": "..."} — common dict shape
        if "content" in value:
            role = value.get("role")
            inner = _extract_text(value.get("content"))
            return f"{role}: {inner}" if role else inner
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        # Fallback: serialize to JSON for readability
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    # ChatMessage / Message-like objects
    content = getattr(value, "content", None)
    if content is not None:
        role = getattr(value, "role", None)
        if role:
            return f"{role}: {_extract_text(content)}"
        return _extract_text(content)
    # ChatResponse — has .message which itself has .content
    message = getattr(value, "message", None)
    if message is not None:
        return _extract_text(message)
    # Plain text-like — e.g. CompletionResponse has .text
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    # Last resort: best-effort string repr (still readable in dashboard)
    return str(value)


def _first_present(*values):
    """Return the first non-None value. Unlike ``a or b``, this
    preserves explicit zero values (e.g. fully-cached prompts that
    legitimately report ``prompt_tokens=0``)."""
    for v in values:
        if v is not None:
            return v
    return None


def _usage_from_dict(usage: dict) -> tuple:
    """Pick prompt/completion or input/output tokens from a usage
    dict, preserving zero values."""
    return (
        _first_present(usage.get("prompt_tokens"), usage.get("input_tokens")),
        _first_present(usage.get("completion_tokens"), usage.get("output_tokens")),
    )


def _extract_token_counts(payload: Optional[dict]) -> tuple:
    """Pull (input_tokens, output_tokens) from an LLM end payload.
    LlamaIndex puts usage in different places depending on which LLM
    integration is in play — try the common ones, in priority order."""
    if not payload:
        return None, None

    response = payload.get(EventPayload.RESPONSE)
    if response is not None:
        # LiteLLM-style: response.raw["usage"]
        raw = getattr(response, "raw", None)
        if isinstance(raw, dict):
            usage = raw.get("usage")
            if isinstance(usage, dict):
                return _usage_from_dict(usage)
        # Direct attribute on response.raw (anthropic-style typed obj)
        if raw is not None:
            usage = getattr(raw, "usage", None)
            if usage is not None:
                tin = _first_present(
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "input_tokens", None),
                )
                tout = _first_present(
                    getattr(usage, "completion_tokens", None),
                    getattr(usage, "output_tokens", None),
                )
                if tin is not None or tout is not None:
                    return tin, tout
        # Some integrations stash on response.additional_kwargs
        addl = getattr(response, "additional_kwargs", None)
        if isinstance(addl, dict):
            usage = addl.get("usage")
            if isinstance(usage, dict):
                return _usage_from_dict(usage)

    # Older payloads pass token counts on a top-level additional_kwargs
    addl = payload.get(EventPayload.ADDITIONAL_KWARGS)
    if isinstance(addl, dict):
        usage = addl.get("usage")
        if isinstance(usage, dict):
            return _usage_from_dict(usage)
    return None, None


def _extract_model(payload: Optional[dict]) -> Optional[str]:
    if not payload:
        return None
    name = payload.get(EventPayload.MODEL_NAME)
    if isinstance(name, str) and name:
        return name
    serialized = payload.get(EventPayload.SERIALIZED)
    if isinstance(serialized, dict):
        for key in ("model", "model_name", "deployment_name"):
            v = serialized.get(key)
            if isinstance(v, str) and v:
                return v
    response = payload.get(EventPayload.RESPONSE)
    if response is not None:
        m = getattr(response, "model", None)
        if isinstance(m, str) and m:
            return m
        raw = getattr(response, "raw", None)
        if isinstance(raw, dict):
            m = raw.get("model")
            if isinstance(m, str) and m:
                return m
    return None


def _extract_retrieval_output(payload: Optional[dict]) -> tuple:
    """Returns (output_text, node_count, top_score). For RETRIEVE
    events. Each NodeWithScore has get_text() and score."""
    if not payload:
        return "", 0, None
    nodes = payload.get(EventPayload.NODES) or []
    pieces: List[str] = []
    top_score: Optional[float] = None
    for n in nodes:
        try:
            text = n.get_text() if hasattr(n, "get_text") else str(n)
            score = getattr(n, "score", None)
            if score is not None and (top_score is None or score > top_score):
                top_score = score
            score_str = f" (score={score:.3f})" if score is not None else ""
            pieces.append(f"- {text}{score_str}")
        except Exception:
            pieces.append(str(n))
    return "\n".join(pieces), len(nodes), top_score


# --- The handler ---------------------------------------------------


class SynapticCallbackHandler(BaseCallbackHandler):
    """LlamaIndex BaseCallbackHandler that ships every event to
    Synaptic as a span."""

    def __init__(
        self,
        event_starts_to_ignore: Optional[List[CBEventType]] = None,
        event_ends_to_ignore: Optional[List[CBEventType]] = None,
        project: Optional[str] = None,
    ) -> None:
        super().__init__(
            event_starts_to_ignore=event_starts_to_ignore or [],
            event_ends_to_ignore=event_ends_to_ignore or [],
        )
        self._project = project
        # event_id → span. LlamaIndex supplies parent_id on every
        # event start, so we just need an event_id → span lookup
        # to find spans later when closing them.
        self._spans: Dict[str, _ExtSpan] = {}
        # Trace lifecycle is tracked but doesn't emit a synthetic
        # root span — LlamaIndex itself emits a top-level QUERY (or
        # similar) event that becomes the root. Creating an extra
        # span named after the trace_id (e.g. "query") just produced
        # a confusing duplicate row in the dashboard.
        self._active_traces: Dict[str, Optional[_ExtSpan]] = {}

    # -- Trace lifecycle ---------------------------------------------

    def start_trace(self, trace_id: Optional[str] = None) -> None:
        try:
            trace_key = trace_id or "default"
            # Snapshot the outer @synaptic.trace span (if any) so
            # the first event inside this trace can attach to it.
            outer = get_current_span()
            outer_ext = (
                _ExtSpan(
                    id=outer.id,
                    trace_id=outer.trace_id,
                    parent_span_id=None,
                    name="proxy",
                    type="custom",
                )
                if outer is not None
                else None
            )
            if outer_ext is not None:
                outer_ext.session_id = getattr(outer, "session_id", None)
            self._active_traces[trace_key] = outer_ext
        except Exception:
            # Rule 7: never propagate to the agent
            pass

    def end_trace(
        self,
        trace_id: Optional[str] = None,
        trace_map: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        try:
            trace_key = trace_id or "default"
            self._active_traces.pop(trace_key, None)
            # Reap any spans whose start fired but whose end never
            # arrived — typically because an exception unwound the
            # call stack past the CallbackManager. Without this, the
            # _spans dict leaks unboundedly in long-running apps.
            #
            # We can only reap spans we know belong to this trace.
            # trace_map gives us every event_id seen during the
            # trace (as keys and as members of value lists). If the
            # SDK didn't supply trace_map, leave the entries — they
            # may belong to a still-open concurrent trace.
            if trace_map is None:
                return
            event_ids: set = set()
            for parent, children in trace_map.items():
                event_ids.add(parent)
                for c in children or []:
                    event_ids.add(c)
            sdk = _get_sdk()
            for eid in list(self._spans.keys()):
                if eid in event_ids:
                    leaked = self._spans.pop(eid, None)
                    if leaked is not None:
                        if leaked.error is None:
                            leaked.error = (
                                "Span did not receive on_event_end "
                                "(likely an exception unwound the stack)."
                            )
                        leaked.end()
                        try:
                            sdk.submit(leaked)
                        except Exception:
                            pass
        except Exception:
            pass

    # -- Event lifecycle ---------------------------------------------

    def on_event_start(
        self,
        event_type: CBEventType,
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        parent_id: str = "",
        **kwargs: Any,
    ) -> str:
        try:
            span_type, span_name = _classify(event_type)

            parent = self._spans.get(parent_id) if parent_id else None
            if parent is None:
                # No event-level parent — attach to the outer
                # @synaptic.trace span if start_trace recorded one.
                # Otherwise this event becomes a root span.
                for outer in self._active_traces.values():
                    if outer is not None:
                        parent = outer
                        break

            new_id = str(uuid4())
            if parent is not None:
                trace_id = parent.trace_id
                parent_span_id = parent.id
                session_id = getattr(parent, "session_id", None)
            else:
                trace_id = new_id
                parent_span_id = None
                session_id = None

            span = _ExtSpan(
                id=new_id,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                name=span_name,
                type=span_type,
            )
            span.session_id = session_id

            # Capture the input now — outputs come on end.
            self._capture_start_payload(span, event_type, payload)
            self._spans[event_id] = span
        except Exception:
            pass
        return event_id

    def on_event_end(
        self,
        event_type: CBEventType,
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        try:
            span = self._spans.pop(event_id, None)
            if span is None:
                return
            self._capture_end_payload(span, event_type, payload)
            span.end()
            _get_sdk().submit(span)
        except Exception:
            pass

    # -- Payload helpers ---------------------------------------------

    def _capture_start_payload(
        self,
        span: _ExtSpan,
        event_type: CBEventType,
        payload: Optional[Dict[str, Any]],
    ) -> None:
        if not payload:
            return
        if event_type == CBEventType.LLM:
            messages = payload.get(EventPayload.MESSAGES)
            prompt = payload.get(EventPayload.PROMPT)
            text = _extract_text(messages) if messages is not None else _extract_text(prompt)
            span.input = _serialize(text)
            model = _extract_model(payload)
            if model:
                span.model = model
                span.provider = _provider_from_model(model)
        elif event_type == CBEventType.RETRIEVE:
            q = payload.get(EventPayload.QUERY_STR)
            span.input = _serialize(_extract_text(q))
        elif event_type == CBEventType.EMBEDDING:
            chunks = payload.get(EventPayload.CHUNKS)
            if chunks is not None:
                span.input = _serialize(_extract_text(chunks))
            model = _extract_model(payload)
            if model:
                span.model = model
                span.provider = _provider_from_model(model)
        elif event_type == CBEventType.QUERY:
            q = payload.get(EventPayload.QUERY_STR)
            span.input = _serialize(_extract_text(q))
        elif event_type == CBEventType.FUNCTION_CALL:
            tool = payload.get(EventPayload.TOOL)
            # LlamaIndex tools surface their name in either tool.name
            # (older) or tool.metadata.name (newer). Try both.
            for path in ((tool, "name"), (getattr(tool, "metadata", None), "name")):
                obj, attr = path
                v = getattr(obj, attr, None)
                if isinstance(v, str) and v:
                    span.tool_name = v
                    break
            fn_call = payload.get(EventPayload.FUNCTION_CALL)
            if fn_call is not None:
                span.input = _serialize(fn_call)

    def _capture_end_payload(
        self,
        span: _ExtSpan,
        event_type: CBEventType,
        payload: Optional[Dict[str, Any]],
    ) -> None:
        # If the operation failed, LlamaIndex's CallbackManager calls
        # on_event_end with payload={EXCEPTION: e} on the failing
        # event's id — regardless of event_type. Capture the error
        # on the span for ANY event that ended with an exception.
        if payload and EventPayload.EXCEPTION in payload:
            exc = payload[EventPayload.EXCEPTION]
            if isinstance(exc, BaseException):
                span.error = f"{type(exc).__name__}: {exc}"
            elif exc is not None:
                span.error = str(exc)
            # Don't return — still try to capture any other fields
            # the payload happened to include.

        if not payload:
            return

        if event_type == CBEventType.LLM:
            response = payload.get(EventPayload.RESPONSE)
            completion = payload.get(EventPayload.COMPLETION)
            text = _extract_text(response) if response is not None else _extract_text(completion)
            span.output = _serialize(text)

            # Token usage: prefer the LLM's reported numbers; estimate
            # from text length only when the SDK doesn't supply them.
            tin, tout = _extract_token_counts(payload)
            span.tokens_input = tin
            span.tokens_output = tout
            if span.tokens_input is None and span.input:
                span.tokens_input = _estimate_tokens(span.input)
            if span.tokens_output is None and span.output:
                span.tokens_output = _estimate_tokens(span.output)

            # Model may only be available on response (some LLMs)
            if not span.model:
                model = _extract_model(payload)
                if model:
                    span.model = model
                    span.provider = _provider_from_model(model)
            span.cost_usd = _compute_cost(span.model, span.tokens_input, span.tokens_output)
        elif event_type == CBEventType.RETRIEVE:
            text, count, top_score = _extract_retrieval_output(payload)
            span.output = _serialize(text)
        elif event_type == CBEventType.EMBEDDING:
            # LlamaIndex passes the actual chunks/text only on end —
            # capture as input here if start_payload didn't have it.
            chunks = payload.get(EventPayload.CHUNKS)
            if chunks is not None and not span.input:
                span.input = _serialize(_extract_text(chunks))
            embeddings = payload.get(EventPayload.EMBEDDINGS) or []
            # Don't dump the full vectors — just record the count
            span.output = _serialize(f"{len(embeddings)} embedding vector(s)")
            if span.input and span.tokens_input is None:
                span.tokens_input = _estimate_tokens(span.input)
            if span.model and span.tokens_input is not None:
                span.cost_usd = _compute_cost(span.model, span.tokens_input, 0)
        elif event_type == CBEventType.QUERY:
            response = payload.get(EventPayload.RESPONSE)
            span.output = _serialize(_extract_text(response))
        elif event_type == CBEventType.FUNCTION_CALL:
            out = payload.get(EventPayload.FUNCTION_OUTPUT)
            if out is not None:
                span.output = _serialize(out)


def _provider_from_model(model: str) -> Optional[str]:
    """Best-effort provider inference from a model name. Used only
    when llama-index doesn't tell us directly."""
    m = model.lower()
    if m.startswith("gpt-") or m.startswith("text-embedding"):
        return "openai"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini") or m.startswith("models/"):
        return "google"
    if m.startswith("command"):
        return "cohere"
    if "/" in m:
        return m.split("/", 1)[0]
    return None


# --- Zero-config auto-registration ---------------------------------


def _maybe_register_global() -> bool:
    """If ``SYNAPTIC_TRACING=true`` and llama-index's ``Settings``
    object is available, attach our handler to the global callback
    manager. Idempotent — already-installed handlers aren't added
    twice.

    Returns True if installed, False if disabled or unreachable.
    """
    val = (os.environ.get("SYNAPTIC_TRACING") or "").lower()
    if val not in ("true", "1"):
        return False
    try:
        from llama_index.core import Settings
        from llama_index.core.callbacks import CallbackManager
    except Exception:
        return False
    try:
        existing = Settings.callback_manager
        existing_handlers = getattr(existing, "handlers", []) or []
        if any(isinstance(h, SynapticCallbackHandler) for h in existing_handlers):
            return True
        # Prefer add_handler so we don't lose subclass identity or
        # extra state on the user's CallbackManager. Fall back to
        # replacing only if add_handler isn't exposed on this version.
        if hasattr(existing, "add_handler"):
            existing.add_handler(SynapticCallbackHandler())
        else:
            new_handlers = list(existing_handlers) + [SynapticCallbackHandler()]
            Settings.callback_manager = CallbackManager(new_handlers)
        return True
    except Exception:
        return False


# Run at import time. Failure is silent — Rule 7.
_maybe_register_global()


__all__ = ["SynapticCallbackHandler", "register_model_price"]
