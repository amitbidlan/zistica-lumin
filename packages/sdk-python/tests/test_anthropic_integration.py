"""Tests for the Anthropic integration — extended thinking visualization."""

import asyncio
import json
from types import SimpleNamespace
from typing import Optional

import pytest


pytest.importorskip("anthropic")

from lumin.integrations.anthropic import (  # noqa: E402
    _compute_cost,
    instrument_anthropic,
)


# ---------- stand-in Anthropic-shaped classes ----------


class _FakeBlock:
    def __init__(self, type: str, **fields):
        self.type = type
        for k, v in fields.items():
            setattr(self, k, v)


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, content, input_tokens=10, output_tokens=20):
        self.content = content
        self.usage = _FakeUsage(input_tokens, output_tokens)


def _make_messages_class(response_factory):
    class FakeMessages:
        def create(self, *args, **kwargs):
            return response_factory(**kwargs)

    return FakeMessages


def _make_async_messages_class(response_factory):
    class FakeAsyncMessages:
        async def create(self, *args, **kwargs):
            return response_factory(**kwargs)

    return FakeAsyncMessages


# ---------- helpers ----------


def _drain(sdk):
    sdk.flush()
    return sdk._exporter.spans


# ---------- cost / model unit tests ----------


def test_compute_cost_known_claude_models():
    # Opus: 0.015 input, 0.075 output per 1K
    assert _compute_cost("claude-opus-4-20250514", 1000, 1000) == pytest.approx(
        0.090, rel=1e-3
    )


def test_compute_cost_returns_none_for_unknown_model():
    assert _compute_cost("not-a-claude-model", 100, 100) is None


def test_compute_cost_handles_zero_tokens():
    # Thinking spans use 0 input — should still compute a value
    assert _compute_cost("claude-opus-4", 0, 1000) == pytest.approx(0.075, rel=1e-3)


# ---------- instrumented call: thinking + response ----------


def _response_with_thinking(**_):
    return _FakeResponse(
        content=[
            _FakeBlock(
                "thinking",
                thinking="Let me work through this carefully. 2+2 means combining two and two...",
            ),
            _FakeBlock("text", text="2+2 equals 4."),
        ],
        input_tokens=12,
        output_tokens=400,
    )


def _response_no_thinking(**_):
    return _FakeResponse(
        content=[_FakeBlock("text", text="hello")],
        input_tokens=5,
        output_tokens=2,
    )


def test_call_with_thinking_emits_parent_thinking_response_spans(sdk):
    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    client_messages = Cls()
    response = client_messages.create(
        model="claude-opus-4-20250514",
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": 10000},
        messages=[{"role": "user", "content": "What is 2+2?"}],
    )
    assert response.content[1].text == "2+2 equals 4."

    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    assert "claude_call" in by_name
    assert "thinking" in by_name
    assert "response" in by_name

    parent = by_name["claude_call"]
    thinking = by_name["thinking"]
    answer = by_name["response"]

    assert parent.parent_span_id is None
    assert thinking.parent_span_id == parent.id
    assert answer.parent_span_id == parent.id
    assert thinking.trace_id == parent.trace_id == answer.trace_id


def test_thinking_span_metadata(sdk):
    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(
        model="claude-opus-4-20250514",
        messages=[{"role": "user", "content": "x"}],
    )
    by_name = {s.name: s for s in _drain(sdk)}
    thinking = by_name["thinking"]
    d = thinking.to_dict()

    assert d["span_subtype"] == "thinking"
    assert d["model"] == "claude-opus-4-20250514"
    assert d["provider"] == "anthropic"
    assert d["thinking_tokens"] is not None
    assert d["thinking_tokens"] > 0
    assert d["cost_usd"] is not None
    # Thinking content is in the input field so the dashboard's
    # expand-input pattern shows it
    assert "Let me work through this" in (d["input"] or "")


def test_response_span_has_only_response_text_and_subtracted_tokens(sdk):
    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(model="claude-opus-4-20250514", messages=[])
    by_name = {s.name: s for s in _drain(sdk)}
    answer = by_name["response"]
    parent = by_name["claude_call"]

    assert answer.span_subtype == "response"
    assert "2+2 equals 4" in (answer.output or "")
    # Response tokens = parent.output_tokens - thinking estimate
    assert answer.tokens_output is not None
    assert answer.tokens_output >= 0
    assert answer.tokens_output < parent.tokens_output


def test_call_without_thinking_emits_only_parent_and_response(sdk):
    Cls = _make_messages_class(_response_no_thinking)
    AsyncCls = _make_async_messages_class(_response_no_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(
        model="claude-haiku-4",
        messages=[{"role": "user", "content": "hi"}],
    )

    by_name = {s.name: s for s in _drain(sdk)}
    assert "thinking" not in by_name
    assert "claude_call" in by_name
    assert "response" in by_name
    assert by_name["claude_call"].thinking_tokens is None


def test_idempotent_instrument(sdk):
    Cls = _make_messages_class(_response_no_thinking)
    AsyncCls = _make_async_messages_class(_response_no_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(model="claude-haiku-4", messages=[])
    by_name = {}
    for s in _drain(sdk):
        by_name.setdefault(s.name, []).append(s)
    # Each name should appear exactly once — no double-wrapping
    assert len(by_name["claude_call"]) == 1
    assert len(by_name["response"]) == 1


def test_unknown_model_does_not_crash(sdk):
    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(model="some-future-model-2026", messages=[])
    by_name = {s.name: s for s in _drain(sdk)}
    parent = by_name["claude_call"]
    # Cost is None because the model isn't in the price table — but
    # everything else still recorded
    assert parent.cost_usd is None
    assert parent.model == "some-future-model-2026"


def test_dict_shaped_thinking_block_also_recognized(sdk):
    """ChatAnthropic via LangChain emits dict-shaped content blocks;
    direct Anthropic SDK emits typed objects. The integration must
    handle either."""
    def factory(**_):
        # dict-shaped blocks (like what LangChain emits)
        return SimpleNamespace(
            content=[
                {"type": "thinking", "thinking": "dict-shaped reasoning"},
                {"type": "text", "text": "dict-shaped answer"},
            ],
            usage=_FakeUsage(input_tokens=5, output_tokens=10),
        )

    Cls = _make_messages_class(factory)
    AsyncCls = _make_async_messages_class(factory)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(model="claude-opus-4", messages=[])
    by_name = {s.name: s for s in _drain(sdk)}
    assert "thinking" in by_name
    assert "dict-shaped reasoning" in (by_name["thinking"].input or "")


def test_async_create_also_instrumented(sdk):
    async def main():
        Cls = _make_messages_class(_response_with_thinking)
        AsyncCls = _make_async_messages_class(_response_with_thinking)
        instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)
        return await AsyncCls().create(
            model="claude-opus-4",
            messages=[{"role": "user", "content": "x"}],
        )

    asyncio.run(main())
    by_name = {s.name: s for s in _drain(sdk)}
    assert "claude_call" in by_name
    assert "thinking" in by_name
    assert "response" in by_name


def test_instrument_inside_lumin_trace_links_under_outer(sdk):
    """When the user wraps a Claude call in @lumin.trace, the
    claude_call span should appear as a child of the outer span via
    SDK contextvars (the same fallback the LangChain integration uses)."""
    import lumin

    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    @lumin.trace
    def my_agent(q: str) -> str:
        Cls().create(
            model="claude-opus-4",
            messages=[{"role": "user", "content": q}],
        )
        return "done"

    my_agent("test")
    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}

    outer = by_name["my_agent"]
    parent = by_name["claude_call"]
    thinking = by_name["thinking"]

    # claude_call should be a child of my_agent (same trace_id)
    assert parent.trace_id == outer.trace_id
    assert parent.parent_span_id == outer.id
    # thinking still child of claude_call
    assert thinking.parent_span_id == parent.id


def test_streaming_records_spans_when_context_exits(sdk):
    """A user calling client.messages.stream(...) gets the same span
    structure as create() once the stream context exits."""
    final_response = _FakeResponse(
        content=[
            _FakeBlock("thinking", thinking="streaming reasoning"),
            _FakeBlock("text", text="streamed answer"),
        ],
        input_tokens=10, output_tokens=50,
    )

    class FakeStream:
        def get_final_message(self):
            return final_response

    class FakeStreamManager:
        def __init__(self):
            self._MessageStreamManager__stream = FakeStream()
        def __enter__(self):
            return self._MessageStreamManager__stream
        def __exit__(self, *args):
            return None

    class FakeMessages:
        def create(self, *args, **kwargs):
            return _response_no_thinking()
        def stream(self, *args, **kwargs):
            return FakeStreamManager()

    AsyncCls = type("X", (), {
        "create": lambda self, *a, **k: _response_no_thinking(),
        "stream": lambda self, *a, **k: FakeStreamManager(),
    })
    instrument_anthropic(messages_cls=FakeMessages, async_messages_cls=AsyncCls)

    with FakeMessages().stream(model="claude-opus-4", messages=[]) as stream:
        # User would normally iterate events here
        pass

    by_name = {s.name: s for s in _drain(sdk)}
    assert "claude_call" in by_name
    assert "thinking" in by_name
    assert "response" in by_name
    assert "streaming reasoning" in by_name["thinking"].input
    assert "streamed answer" in by_name["response"].output


def test_streaming_does_not_record_when_user_exits_with_exception(sdk):
    """If the user's stream block raises, we don't record a half-baked span."""
    class FakeStream:
        def get_final_message(self):
            raise RuntimeError("should not be called when exiting with exception")

    class FakeStreamManager:
        def __init__(self):
            self._MessageStreamManager__stream = FakeStream()
        def __enter__(self):
            return self._MessageStreamManager__stream
        def __exit__(self, *args):
            return None

    class FakeMessages:
        def create(self, *args, **kwargs):
            return _response_no_thinking()
        def stream(self, *args, **kwargs):
            return FakeStreamManager()

    AsyncCls = type("X", (), {
        "create": lambda self, *a, **k: _response_no_thinking(),
        "stream": lambda self, *a, **k: FakeStreamManager(),
    })
    instrument_anthropic(messages_cls=FakeMessages, async_messages_cls=AsyncCls)

    try:
        with FakeMessages().stream(model="claude-opus-4", messages=[]):
            raise ValueError("user-raised")
    except ValueError:
        pass

    spans = _drain(sdk)
    # No spans recorded — the failed stream is skipped
    assert len(spans) == 0


def test_child_spans_temporally_nested_inside_parent(sdk):
    """Regression for a bug found by trace audit — every child's
    [started_at, ended_at] interval must fall inside the parent's
    interval. Originally children were created with `now()` after
    `parent.end()` ran, putting them strictly after the parent's
    bracket and breaking the timeline-nesting invariant."""
    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(model="claude-opus-4-20250514", messages=[])
    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    parent = by_name["claude_call"]
    for child_name in ("thinking", "response"):
        if child_name not in by_name:
            continue
        c = by_name[child_name]
        assert c.started_at >= parent.started_at, (
            f"{child_name}.started_at={c.started_at} < parent.started_at={parent.started_at}"
        )
        assert c.ended_at <= parent.ended_at, (
            f"{child_name}.ended_at={c.ended_at} > parent.ended_at={parent.ended_at}"
        )
    # And: thinking ends where response starts (sequential, not overlapping)
    if "thinking" in by_name and "response" in by_name:
        assert by_name["thinking"].ended_at == by_name["response"].started_at


def test_to_dict_shape_for_thinking_span(sdk):
    Cls = _make_messages_class(_response_with_thinking)
    AsyncCls = _make_async_messages_class(_response_with_thinking)
    instrument_anthropic(messages_cls=Cls, async_messages_cls=AsyncCls)

    Cls().create(model="claude-opus-4", messages=[])
    by_name = {s.name: s for s in _drain(sdk)}
    thinking = by_name["thinking"]
    d = thinking.to_dict()
    # The shared _ExtSpan should emit all expected keys
    for key in (
        "id", "trace_id", "parent_span_id", "name", "type",
        "input", "output", "started_at", "ended_at", "error",
        "session_id", "model", "provider", "tokens_input",
        "tokens_output", "cost_usd", "tool_name",
        "span_subtype", "thinking_tokens",
    ):
        assert key in d, f"missing key: {key}"
