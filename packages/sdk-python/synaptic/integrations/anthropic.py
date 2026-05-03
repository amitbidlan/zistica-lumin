"""Anthropic SDK integration for Synaptic.

Wraps ``Anthropic.messages.create`` and the async equivalent so each
Claude call records:

  - A parent ``claude_call`` span (type=llm) with model, tokens, cost
  - One ``thinking`` child span per thinking block in the response
    (subtype=\"thinking\", carries the thinking text + estimated tokens
    + cost computed at output rates)
  - One ``response`` child span (subtype=\"response\") with the final
    text content

This is the Claude-first observability path — visible thinking is the
whole point of extended thinking, and Synaptic surfaces it as a
first-class child span instead of dropping it.

Usage:

    from synaptic.integrations.anthropic import instrument_anthropic
    instrument_anthropic()

    from anthropic import Anthropic
    client = Anthropic()
    response = client.messages.create(
        model=\"claude-opus-4-20250514\",
        max_tokens=16000,
        thinking={\"type\": \"enabled\", \"budget_tokens\": 10000},
        messages=[{\"role\": \"user\", \"content\": \"What is 2+2?\"}],
    )
"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable, Iterable, List, Optional, Tuple
from uuid import uuid4

try:
    from anthropic import Anthropic, AsyncAnthropic  # noqa: F401
    from anthropic.resources.messages import AsyncMessages, Messages
except ImportError as e:
    raise ImportError(
        "synaptic.integrations.anthropic requires the anthropic SDK. "
        "Install with: pip install anthropic"
    ) from e

from synaptic.context import get_current_span
from synaptic.integrations._ext_span import _ExtSpan, _estimate_tokens, _serialize
from synaptic.sdk import _get_sdk


# Anthropic Claude pricing per 1K tokens (May 2026; see also
# integrations/langchain.py — kept independent here so this file
# doesn't reach into the LangChain price table).
PRICES_PER_1K = {
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-haiku-4": (0.001, 0.005),
}


def _compute_cost(model: Optional[str], tin: Optional[int], tout: Optional[int]) -> Optional[float]:
    if not model or tin is None or tout is None:
        return None
    m = model.lower()
    best: Optional[Tuple[float, float]] = None
    best_key = ""
    for key, prices in PRICES_PER_1K.items():
        if m.startswith(key) and len(key) > len(best_key):
            best = prices
            best_key = key
    if best is None:
        return None
    inp, outp = best
    return round(tin * inp / 1000 + tout * outp / 1000, 8)


def _make_span(
    parent: Optional[_ExtSpan],
    name: str,
    type: str = "llm",
    subtype: Optional[str] = None,
) -> _ExtSpan:
    """Build a child span anchored at `parent`, or a root if none given."""
    new_id = str(uuid4())
    if parent is None:
        trace_id = new_id
        parent_span_id = None
        session_id = None
    else:
        trace_id = parent.trace_id
        parent_span_id = parent.id
        session_id = getattr(parent, "session_id", None)

    s = _ExtSpan(
        id=new_id,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        name=name,
        type=type,
    )
    s.span_subtype = subtype
    s.session_id = session_id
    return s


def _summarize_messages(messages: Optional[Iterable[Any]]) -> List[dict]:
    """Render the request messages list down to {role, content} dicts."""
    if not messages:
        return []
    out: List[dict] = []
    for m in messages:
        if isinstance(m, dict):
            out.append({"role": m.get("role"), "content": m.get("content")})
        else:
            # Be defensive for typed message objects
            out.append(
                {
                    "role": getattr(m, "role", None),
                    "content": getattr(m, "content", None),
                }
            )
    return out


def _extract_blocks(response: Any) -> Tuple[List[Any], List[Any]]:
    """Split response.content into (thinking_blocks, text_blocks)."""
    content = getattr(response, "content", None) or []
    thinking: List[Any] = []
    text: List[Any] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type == "thinking":
            thinking.append(block)
        elif block_type == "text":
            text.append(block)
    return thinking, text


def _midpoint(start: Optional[str], end: Optional[str], fraction: float = 0.5) -> Optional[str]:
    """Return the ISO-8601 timestamp `fraction` of the way from
    `start` to `end`. Used to split a parent span's interval into
    sequential child intervals when Anthropic doesn't tell us the
    exact thinking-vs-response boundary."""
    if start is None or end is None:
        return end or start
    try:
        from datetime import datetime
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = (e - s) * fraction
        return (s + delta).isoformat()
    except Exception:
        return end


def _block_text(block: Any, key: str) -> str:
    """Pull the inner text from a content block — works for both the
    typed ``ContentBlock`` objects and dict-shaped blocks."""
    val = getattr(block, key, None)
    if val is None and isinstance(block, dict):
        val = block.get(key, "")
    return val or ""


def _record_call(
    *,
    model: Optional[str],
    request_messages: Optional[Iterable[Any]],
    response: Any,
    duration_ms: int,
) -> None:
    """Build the parent + children spans from a successful Claude call
    and submit them. Failures are swallowed."""
    try:
        parent_in_context = get_current_span()
        # Cast to _ExtSpan-shaped if it is one — we only need session_id
        parent_ext = parent_in_context if isinstance(parent_in_context, _ExtSpan) else None

        # Build the parent claude_call span
        # Note: when the user already has @synaptic.trace open, we still
        # create our own span so the call is attributable specifically.
        # Its parent_span_id points at the outer @trace span.
        outer_for_link = _ExtSpan(
            id=parent_in_context.id, trace_id=parent_in_context.trace_id,
            parent_span_id=None, name="proxy", type="custom",
        ) if parent_in_context is not None else None

        parent = _make_span(outer_for_link, "claude_call", type="llm")
        parent.model = model
        parent.provider = "anthropic"
        parent.input = _serialize({"messages": _summarize_messages(request_messages)})
        if parent_ext is not None and parent_ext.session_id:
            parent.session_id = parent_ext.session_id

        thinking_blocks, text_blocks = _extract_blocks(response)
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None

        parent.tokens_input = input_tokens
        parent.tokens_output = output_tokens
        parent.cost_usd = _compute_cost(model, input_tokens, output_tokens)

        # Estimated thinking tokens (Anthropic doesn't break them out
        # in usage — they're rolled into output_tokens).
        thinking_text_total = "".join(_block_text(b, "thinking") for b in thinking_blocks)
        thinking_tokens_est = _estimate_tokens(thinking_text_total)
        if thinking_blocks:
            parent.thinking_tokens = thinking_tokens_est

        # Parent's "output" is the final text (so it shows something
        # useful when a user expands the parent without drilling in).
        response_text = "".join(_block_text(b, "text") for b in text_blocks)
        parent.output = _serialize({"text": response_text})
        parent.end()
        # Snapshot parent's interval. Children must fall WITHIN it
        # so trace timelines render correctly. Without this, each
        # child gets `now()` for started_at — strictly *after*
        # parent.ended_at — and audit invariants break.
        parent_started = parent.started_at
        parent_ended = parent.ended_at

        sdk = _get_sdk()
        sdk.submit(parent)

        # Thinking span: one combined span aggregating all thinking
        # blocks. Anthropic doesn't break thinking out of the wall
        # clock — we don't know exactly when thinking ended and
        # response began. Treat them as a sequence inside the
        # parent's interval: thinking covers the FIRST 80% of the
        # call (Claude spends most of the latency reasoning), and
        # the response span covers the LAST 20%. This is a heuristic
        # but it keeps the visual ordering correct in the timeline.
        thinking_split = _midpoint(parent_started, parent_ended, 0.8)

        if thinking_blocks:
            thinking_span = _make_span(parent, "thinking", type="llm", subtype="thinking")
            thinking_span.model = model
            thinking_span.provider = "anthropic"
            thinking_span.thinking_tokens = thinking_tokens_est
            thinking_span.input = _serialize({"thinking": thinking_text_total})
            # Thinking is billed at the output rate per Anthropic
            thinking_span.cost_usd = _compute_cost(model, 0, thinking_tokens_est)
            thinking_span.started_at = parent_started
            thinking_span.ended_at = thinking_split
            sdk.submit(thinking_span)

        # Response span: final text answer
        if text_blocks:
            response_span = _make_span(parent, "response", type="llm", subtype="response")
            response_span.model = model
            response_span.provider = "anthropic"
            # Subtract estimated thinking from total output for a rough
            # response-only token count.
            if output_tokens is not None and thinking_tokens_est:
                response_span.tokens_output = max(0, output_tokens - thinking_tokens_est)
            else:
                response_span.tokens_output = output_tokens
            response_span.cost_usd = _compute_cost(
                model, 0, response_span.tokens_output
            )
            response_span.output = _serialize({"text": response_text})
            # If we had thinking, response starts where thinking ended.
            # Otherwise, response covers the full parent interval.
            response_span.started_at = thinking_split if thinking_blocks else parent_started
            response_span.ended_at = parent_ended
            sdk.submit(response_span)
    except Exception:
        # Best-effort — agent must never see exceptions from Synaptic.
        pass


# ---------- Patcher ----------


_PATCH_MARKER = "_synaptic_anthropic_wrapped"


def _wrap_create_sync(original: Callable) -> Callable:
    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        model = kwargs.get("model")
        messages = kwargs.get("messages")
        t0 = time.time()
        try:
            response = original(self, *args, **kwargs)
        except Exception:
            # Don't try to record failed calls for v1 — just propagate
            raise
        duration_ms = int((time.time() - t0) * 1000)
        _record_call(
            model=model,
            request_messages=messages,
            response=response,
            duration_ms=duration_ms,
        )
        return response

    setattr(wrapped, _PATCH_MARKER, True)
    setattr(wrapped, "_synaptic_original", original)
    return wrapped


def _wrap_create_async(original: Callable) -> Callable:
    @functools.wraps(original)
    async def wrapped(self, *args, **kwargs):
        model = kwargs.get("model")
        messages = kwargs.get("messages")
        t0 = time.time()
        response = await original(self, *args, **kwargs)
        duration_ms = int((time.time() - t0) * 1000)
        _record_call(
            model=model,
            request_messages=messages,
            response=response,
            duration_ms=duration_ms,
        )
        return response

    setattr(wrapped, _PATCH_MARKER, True)
    setattr(wrapped, "_synaptic_original", original)
    return wrapped


class _SyncStreamProxy:
    """Wraps an Anthropic MessageStreamManager so that on clean exit we
    snapshot the final Message and record spans. Python looks up
    ``__enter__`` / ``__exit__`` on the type, so we have to use a
    wrapper class instead of patching the instance."""
    def __init__(self, inner: Any, model: Optional[str], messages: Any):
        self._inner = inner
        self._model = model
        self._messages = messages
        self._stream: Any = None

    def __enter__(self):
        self._stream = self._inner.__enter__()
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self._stream is not None:
                final = self._stream.get_final_message()
                _record_call(
                    model=self._model,
                    request_messages=self._messages,
                    response=final,
                    duration_ms=0,
                )
        except Exception:
            pass
        return self._inner.__exit__(exc_type, exc, tb)


class _AsyncStreamProxy:
    def __init__(self, inner: Any, model: Optional[str], messages: Any):
        self._inner = inner
        self._model = model
        self._messages = messages
        self._stream: Any = None

    async def __aenter__(self):
        self._stream = await self._inner.__aenter__()
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self._stream is not None:
                final = await self._stream.get_final_message()
                _record_call(
                    model=self._model,
                    request_messages=self._messages,
                    response=final,
                    duration_ms=0,
                )
        except Exception:
            pass
        return await self._inner.__aexit__(exc_type, exc, tb)


def _wrap_stream_sync(original: Callable) -> Callable:
    """Wrap ``Messages.stream`` so the final Message is recorded when
    the stream context exits. We don't track per-event deltas — the
    final Message has the same shape as a non-stream response."""
    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        manager = original(self, *args, **kwargs)
        return _SyncStreamProxy(
            manager, kwargs.get("model"), kwargs.get("messages")
        )

    setattr(wrapped, _PATCH_MARKER, True)
    setattr(wrapped, "_synaptic_original", original)
    return wrapped


def _wrap_stream_async(original: Callable) -> Callable:
    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        manager = original(self, *args, **kwargs)
        return _AsyncStreamProxy(
            manager, kwargs.get("model"), kwargs.get("messages")
        )

    setattr(wrapped, _PATCH_MARKER, True)
    setattr(wrapped, "_synaptic_original", original)
    return wrapped


def instrument_anthropic(
    messages_cls: Optional[type] = None,
    async_messages_cls: Optional[type] = None,
) -> None:
    """Monkey-patch ``Messages.create``, ``AsyncMessages.create``,
    ``Messages.stream``, and ``AsyncMessages.stream`` so every Claude
    call is recorded as a parent ``claude_call`` span with ``thinking``
    and ``response`` children.

    Idempotent. ``messages_cls`` / ``async_messages_cls`` are accepted
    so tests can pass stand-in classes without importing the real
    Anthropic SDK on every test run.
    """
    sync_target = messages_cls if messages_cls is not None else Messages
    async_target = async_messages_cls if async_messages_cls is not None else AsyncMessages

    sync_create = getattr(sync_target, "create", None)
    if sync_create is not None and not getattr(sync_create, _PATCH_MARKER, False):
        sync_target.create = _wrap_create_sync(sync_create)

    async_create = getattr(async_target, "create", None)
    if async_create is not None and not getattr(async_create, _PATCH_MARKER, False):
        # Only patch if the original is actually a coroutine function —
        # otherwise the user passed a stand-in class with a sync create.
        if inspect.iscoroutinefunction(async_create):
            async_target.create = _wrap_create_async(async_create)
        else:
            async_target.create = _wrap_create_sync(async_create)

    sync_stream = getattr(sync_target, "stream", None)
    if sync_stream is not None and not getattr(sync_stream, _PATCH_MARKER, False):
        sync_target.stream = _wrap_stream_sync(sync_stream)

    async_stream = getattr(async_target, "stream", None)
    if async_stream is not None and not getattr(async_stream, _PATCH_MARKER, False):
        async_target.stream = _wrap_stream_async(async_stream)


__all__ = ["instrument_anthropic"]
