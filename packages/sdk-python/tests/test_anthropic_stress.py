"""Stress / torture tests for the Anthropic integration.

Goal: try the hardest, ugliest inputs we can think of and confirm
the integration still emits sane spans (or fails closed without
breaking the agent — Rule 7)."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("anthropic")

from lumin.integrations.anthropic import (  # noqa: E402
    instrument_anthropic,
    _record_call,
)


# ---------- shapes ----------


class _FakeBlock:
    def __init__(self, type: str, **fields):
        self.type = type
        for k, v in fields.items():
            setattr(self, k, v)


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _resp(content, *, input_tokens=10, output_tokens=20):
    return SimpleNamespace(content=content, usage=_FakeUsage(input_tokens, output_tokens))


def _drain(sdk):
    """Flush until the queue is empty. The SDK's flush() drains at
    most batch_size (default 100) per call — under stress we may
    submit more than that, so loop until nothing more lands."""
    last = -1
    for _ in range(50):
        sdk.flush()
        if len(sdk._exporter.spans) == last:
            break
        last = len(sdk._exporter.spans)
    return sdk._exporter.spans


# ---------- 1. Many thinking blocks in one response ----------


def test_three_thinking_blocks_aggregate_into_one_thinking_span(sdk):
    """Anthropic API allows multiple thinking blocks per response.
    The integration aggregates them into a single thinking span so the
    dashboard renders one row with the combined reasoning."""
    response = _resp(
        content=[
            _FakeBlock("thinking", thinking="Step 1: identify the problem. "),
            _FakeBlock("thinking", thinking="Step 2: enumerate the options. "),
            _FakeBlock("thinking", thinking="Step 3: pick the best."),
            _FakeBlock("text", text="The answer."),
        ],
        input_tokens=10, output_tokens=200,
    )
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)

    by_name = {}
    for s in _drain(sdk):
        by_name.setdefault(s.name, []).append(s)
    # One aggregated thinking span, not three
    assert len(by_name["thinking"]) == 1, f"expected 1 aggregated thinking span, got {len(by_name['thinking'])}"
    thinking = by_name["thinking"][0]
    # Combined text contains all three steps
    assert "Step 1" in thinking.input
    assert "Step 2" in thinking.input
    assert "Step 3" in thinking.input


# ---------- 2. Only thinking, no text ----------


def test_thinking_only_no_text_block_does_not_crash(sdk):
    """Edge case: Claude returns only a thinking block with no text
    (e.g. stop_reason=max_tokens during reasoning). Don't crash."""
    response = _resp(
        content=[_FakeBlock("thinking", thinking="incomplete reasoning")],
        input_tokens=5, output_tokens=100,
    )
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    by_name = {s.name: s for s in _drain(sdk)}
    assert "claude_call" in by_name
    assert "thinking" in by_name
    assert "response" not in by_name


# ---------- 3. Empty content list ----------


def test_empty_content_list_emits_only_parent(sdk):
    response = _resp(content=[], input_tokens=0, output_tokens=0)
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    by_name = {s.name: s for s in _drain(sdk)}
    assert "claude_call" in by_name
    assert "thinking" not in by_name
    assert "response" not in by_name


# ---------- 4. Missing usage object ----------


def test_response_without_usage_object_does_not_crash(sdk):
    response = SimpleNamespace(
        content=[
            _FakeBlock("thinking", thinking="reasoning"),
            _FakeBlock("text", text="answer"),
        ],
        usage=None,
    )
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    by_name = {s.name: s for s in _drain(sdk)}
    assert "claude_call" in by_name
    parent = by_name["claude_call"]
    assert parent.tokens_input is None
    assert parent.tokens_output is None
    # Cost is None when tokens missing
    assert parent.cost_usd is None


# ---------- 5. Pathologically large reasoning ----------


def test_one_megabyte_of_reasoning_is_truncated_not_OOM(sdk):
    """Lumin uses _serialize() which truncates at max_payload_size.
    A 1 MB reasoning string should not blow up memory or queue size."""
    huge_text = "REASONING " * 100_000  # ~1 MB
    response = _resp(
        content=[
            _FakeBlock("thinking", thinking=huge_text),
            _FakeBlock("text", text="answer"),
        ],
        input_tokens=10, output_tokens=100_000,
    )
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    by_name = {s.name: s for s in _drain(sdk)}
    thinking = by_name["thinking"]
    # _serialize default cap is 10240 — anything beyond is dropped on
    # the wire but the in-memory span doesn't hoard the full string
    assert thinking.input is not None
    assert len(thinking.input) <= 12_000, f"thinking.input not truncated: {len(thinking.input)} chars"


# ---------- 6. Unicode + emoji ----------


def test_unicode_and_emoji_round_trip_in_reasoning(sdk):
    weird_text = "推論: なぜ 2+2=4? 🧠 because counting works → ∴ ✓"
    response = _resp(
        content=[
            _FakeBlock("thinking", thinking=weird_text),
            _FakeBlock("text", text="four ✓"),
        ],
    )
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    by_name = {s.name: s for s in _drain(sdk)}
    thinking = by_name["thinking"]
    response_span = by_name["response"]
    assert "推論" in thinking.input
    assert "🧠" in thinking.input
    assert "→" in thinking.input
    assert "four ✓" in response_span.output


# ---------- 7. Original create() raising ----------


def test_exception_in_real_create_propagates_and_no_span_emitted(sdk):
    """If the user's Anthropic call raises (network error, rate limit,
    etc), the exception MUST propagate to the agent — but no broken
    span should land in the exporter (Rule 7)."""
    class FakeMessages:
        def create(self, **_):
            raise RuntimeError("simulated 429 rate limit")

    instrument_anthropic(messages_cls=FakeMessages,
                        async_messages_cls=type("X", (), {"create": FakeMessages.create}))

    with pytest.raises(RuntimeError, match="simulated 429"):
        FakeMessages().create(model="claude-opus-4", messages=[])

    # No spans should land — we don't record failed calls in v1
    spans = _drain(sdk)
    assert len(spans) == 0, f"unexpected spans on failure: {[s.name for s in spans]}"


# ---------- 8. Garbage response shape (defense in depth) ----------


def test_garbage_response_does_not_crash(sdk):
    """If Anthropic somehow returns something with the wrong shape
    (newer SDK with new fields, etc), the integration must swallow
    the error rather than break the agent."""
    response = SimpleNamespace(content="not a list at all", usage=None)
    # This should not raise — _record_call wraps everything in try/except
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    # We don't assert on spans — they may or may not land. The point
    # is that no exception escaped.


# ---------- 9. Concurrent calls from many threads ----------


def test_one_hundred_concurrent_calls_all_emit_correct_spans(sdk):
    """Three child spans must materialize for each call, in correct
    parent-child relationship, even under thread contention."""
    def one_call(i: int):
        response = _resp(
            content=[
                _FakeBlock("thinking", thinking=f"thread {i} reasoning"),
                _FakeBlock("text", text=f"thread {i} answer"),
            ],
            input_tokens=5, output_tokens=20,
        )
        _record_call(model="claude-opus-4",
                     request_messages=[{"role":"user","content":f"q-{i}"}],
                     response=response, duration_ms=1)

    threads = [threading.Thread(target=one_call, args=(i,)) for i in range(100)]
    for t in threads: t.start()
    for t in threads: t.join()

    spans = _drain(sdk)
    # 100 calls × 3 spans = 300 spans
    assert len(spans) == 300, f"expected 300, got {len(spans)}"

    by_name = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)
    assert len(by_name["claude_call"]) == 100
    assert len(by_name["thinking"]) == 100
    assert len(by_name["response"]) == 100

    # Every thinking and response span must have its parent in the parent set
    parent_ids = {p.id for p in by_name["claude_call"]}
    for child in by_name["thinking"] + by_name["response"]:
        assert child.parent_span_id in parent_ids, "child orphaned from any parent"


# ---------- 10. Async fan-out ----------


def test_fifty_concurrent_async_calls(sdk):
    async def one_call(i: int):
        response = _resp(
            content=[
                _FakeBlock("thinking", thinking=f"async-{i}"),
                _FakeBlock("text", text=f"a-{i}"),
            ],
        )
        _record_call(model="claude-opus-4",
                     request_messages=[],
                     response=response, duration_ms=1)
        await asyncio.sleep(0)

    async def main():
        await asyncio.gather(*[one_call(i) for i in range(50)])
    asyncio.run(main())

    spans = _drain(sdk)
    assert len(spans) == 150


# ---------- 11. Double-instrumentation idempotency under attack ----------


def test_instrument_50_times_still_only_one_layer(sdk):
    """Defensive: a user who calls instrument_anthropic() in a hot
    reload loop shouldn't get 50× duplicate spans per call."""
    class FakeMessages:
        def create(self, **_):
            return _resp(
                content=[
                    _FakeBlock("thinking", thinking="r"),
                    _FakeBlock("text", text="a"),
                ],
            )

    AsyncCls = type("X", (), {"create": FakeMessages.create})

    for _ in range(50):
        instrument_anthropic(messages_cls=FakeMessages, async_messages_cls=AsyncCls)

    FakeMessages().create(model="claude-opus-4", messages=[])

    spans = _drain(sdk)
    by_name = {s.name: [] for s in spans}
    for s in spans:
        by_name[s.name].append(s)
    assert len(by_name["claude_call"]) == 1
    assert len(by_name["thinking"]) == 1
    assert len(by_name["response"]) == 1


# ---------- 12. None values in unexpected places ----------


def test_none_in_request_messages_does_not_crash(sdk):
    response = _resp(content=[_FakeBlock("text", text="a")])
    _record_call(model="claude-opus-4", request_messages=None,
                 response=response, duration_ms=1)
    spans = _drain(sdk)
    assert any(s.name == "claude_call" for s in spans)


def test_none_model_handled(sdk):
    response = _resp(content=[_FakeBlock("text", text="a")])
    _record_call(model=None, request_messages=[], response=response, duration_ms=1)
    spans = _drain(sdk)
    parent = next(s for s in spans if s.name == "claude_call")
    # Cost is None when model unknown; no crash
    assert parent.cost_usd is None


# ---------- 13. Tool use blocks alongside thinking (real Claude scenario) ----------


def test_tool_use_block_ignored_thinking_and_text_still_captured(sdk):
    """Claude can return tool_use blocks alongside thinking + text.
    Our integration only consumes thinking + text — others are
    transparently ignored without breaking."""
    response = _resp(
        content=[
            _FakeBlock("thinking", thinking="should I call the tool?"),
            _FakeBlock("tool_use", id="t1", name="get_weather", input={"city":"SF"}),
            _FakeBlock("text", text="Calling get_weather..."),
        ],
        input_tokens=20, output_tokens=80,
    )
    _record_call(model="claude-opus-4", request_messages=[], response=response, duration_ms=1)
    by_name = {s.name: s for s in _drain(sdk)}
    assert "thinking" in by_name
    assert "response" in by_name
    assert "should I call the tool" in by_name["thinking"].input
    assert "get_weather" in by_name["response"].output
