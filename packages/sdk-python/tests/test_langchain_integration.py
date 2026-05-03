"""Tests for the LangChain integration (Session 5)."""

import json
from uuid import uuid4

import pytest

# Skip the whole module if langchain-core isn't available.
pytest.importorskip("langchain_core")

from langchain_core.messages import HumanMessage, AIMessage  # noqa: E402
from langchain_core.outputs import (  # noqa: E402
    ChatGeneration,
    Generation,
    LLMResult,
)

from lumin.integrations.langchain import (  # noqa: E402
    LuminCallbackHandler,
    _compute_cost,
    _ExtSpan,
)


# ---------- helpers ----------


def _serialized_chat_openai(model: str = "gpt-4o") -> dict:
    return {
        "lc": 1,
        "type": "constructor",
        "id": ["langchain", "chat_models", "openai", "ChatOpenAI"],
        "kwargs": {"model_name": model},
        "name": "ChatOpenAI",
    }


def _llm_result(text: str, prompt_tokens: int, completion_tokens: int, model: str = "gpt-4o") -> LLMResult:
    return LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content=text))]],
        llm_output={
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "model_name": model,
        },
    )


def _drain(sdk):
    sdk.flush()
    return sdk._exporter.spans


# ---------- LLM span tests ----------


def test_chat_model_invocation_creates_llm_span(sdk):
    handler = LuminCallbackHandler()
    run_id = uuid4()

    handler.on_chat_model_start(
        serialized=_serialized_chat_openai(),
        messages=[[HumanMessage(content="What is the capital of France?")]],
        run_id=run_id,
    )
    handler.on_llm_end(_llm_result("Paris", prompt_tokens=10, completion_tokens=5), run_id=run_id)

    spans = _drain(sdk)
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "ChatOpenAI"
    assert s.type == "llm"
    assert s.parent_span_id is None
    assert s.error is None


def test_llm_span_has_model_tokens_and_cost(sdk):
    handler = LuminCallbackHandler()
    run_id = uuid4()

    handler.on_chat_model_start(
        serialized=_serialized_chat_openai("gpt-4o"),
        messages=[[HumanMessage(content="hi")]],
        run_id=run_id,
    )
    handler.on_llm_end(_llm_result("hello", prompt_tokens=10, completion_tokens=8, model="gpt-4o"), run_id=run_id)

    s = _drain(sdk)[0]
    d = s.to_dict()

    assert d["model"] == "gpt-4o"
    assert d["provider"] == "openai"
    assert d["tokens_input"] == 10
    assert d["tokens_output"] == 8
    # gpt-4o: 10 * 0.0025/1000 + 8 * 0.010/1000 = 0.000025 + 0.00008 = 0.000105
    assert d["cost_usd"] == pytest.approx(0.000105, rel=1e-3)


def test_completion_style_on_llm_start(sdk):
    """Non-chat LLMs go through on_llm_start, not on_chat_model_start."""
    handler = LuminCallbackHandler()
    run_id = uuid4()

    handler.on_llm_start(
        serialized={"id": ["langchain", "llms", "openai", "OpenAI"], "kwargs": {"model_name": "gpt-3.5-turbo-instruct"}},
        prompts=["Once upon a time"],
        run_id=run_id,
    )
    result = LLMResult(
        generations=[[Generation(text=" there was a llama.")]],
        llm_output={"token_usage": {"prompt_tokens": 4, "completion_tokens": 6}, "model_name": "gpt-3.5-turbo-instruct"},
    )
    handler.on_llm_end(result, run_id=run_id)

    s = _drain(sdk)[0]
    assert s.type == "llm"
    assert s.to_dict()["tokens_output"] == 6


def test_llm_span_with_unknown_model_has_null_cost(sdk):
    handler = LuminCallbackHandler()
    run_id = uuid4()
    handler.on_chat_model_start(
        serialized=_serialized_chat_openai("some-unknown-model-v9"),
        messages=[[HumanMessage(content="x")]],
        run_id=run_id,
    )
    handler.on_llm_end(
        _llm_result("y", prompt_tokens=1, completion_tokens=1, model="some-unknown-model-v9"),
        run_id=run_id,
    )
    s = _drain(sdk)[0]
    assert s.to_dict()["cost_usd"] is None


def test_llm_error_records_error_in_span(sdk):
    handler = LuminCallbackHandler()
    run_id = uuid4()
    handler.on_chat_model_start(
        serialized=_serialized_chat_openai(),
        messages=[[HumanMessage(content="x")]],
        run_id=run_id,
    )
    handler.on_llm_error(RuntimeError("rate limit"), run_id=run_id)

    s = _drain(sdk)[0]
    assert s.error is not None
    assert "RuntimeError" in s.error
    assert "rate limit" in s.error


# ---------- Tool / chain hierarchy tests ----------


def test_tool_call_creates_child_span_under_chain(sdk):
    handler = LuminCallbackHandler()
    chain_id = uuid4()
    tool_id = uuid4()

    handler.on_chain_start(
        serialized={"name": "MyAgent", "id": ["langchain", "agents", "AgentExecutor"]},
        inputs={"query": "what's the weather?"},
        run_id=chain_id,
    )
    handler.on_tool_start(
        serialized={"name": "search_web", "id": ["langchain", "tools", "Tool"]},
        input_str="weather Tokyo",
        inputs={"q": "weather Tokyo"},
        run_id=tool_id,
        parent_run_id=chain_id,
    )
    handler.on_tool_end("sunny, 22C", run_id=tool_id)
    handler.on_chain_end({"output": "The weather is sunny."}, run_id=chain_id)

    spans = {s.name: s for s in _drain(sdk)}
    assert set(spans.keys()) == {"MyAgent", "search_web"}

    chain_span = spans["MyAgent"]
    tool_span = spans["search_web"]

    assert chain_span.parent_span_id is None
    assert tool_span.parent_span_id == chain_span.id
    assert tool_span.trace_id == chain_span.trace_id
    assert tool_span.type == "tool"
    assert tool_span.to_dict()["tool_name"] == "search_web"


def test_three_level_nesting_chain_llm_tool(sdk):
    handler = LuminCallbackHandler()
    chain_id = uuid4()
    llm_id = uuid4()
    tool_id = uuid4()

    handler.on_chain_start(
        serialized={"name": "ResearchAgent"},
        inputs={"q": "x"},
        run_id=chain_id,
    )
    handler.on_chat_model_start(
        serialized=_serialized_chat_openai(),
        messages=[[HumanMessage(content="x")]],
        run_id=llm_id,
        parent_run_id=chain_id,
    )
    handler.on_llm_end(_llm_result("ok", 1, 1), run_id=llm_id)
    handler.on_tool_start(
        serialized={"name": "calculator"},
        input_str="2+2",
        run_id=tool_id,
        parent_run_id=chain_id,
    )
    handler.on_tool_end("4", run_id=tool_id)
    handler.on_chain_end({"output": "done"}, run_id=chain_id)

    spans = {s.name: s for s in _drain(sdk)}
    assert spans["ResearchAgent"].parent_span_id is None
    assert spans["ChatOpenAI"].parent_span_id == spans["ResearchAgent"].id
    assert spans["calculator"].parent_span_id == spans["ResearchAgent"].id
    # All share the trace_id of the root chain
    trace_ids = {s.trace_id for s in spans.values()}
    assert len(trace_ids) == 1


def test_tool_error_records_error(sdk):
    handler = LuminCallbackHandler()
    run_id = uuid4()
    handler.on_tool_start(
        serialized={"name": "failing_tool"},
        input_str="bad",
        run_id=run_id,
    )
    handler.on_tool_error(ValueError("tool blew up"), run_id=run_id)
    s = _drain(sdk)[0]
    assert "ValueError" in s.error
    assert "tool blew up" in s.error


def test_chain_error_records_error(sdk):
    handler = LuminCallbackHandler()
    run_id = uuid4()
    handler.on_chain_start(serialized={"name": "MyChain"}, inputs={}, run_id=run_id)
    handler.on_chain_error(RuntimeError("chain crashed"), run_id=run_id)
    s = _drain(sdk)[0]
    assert "chain crashed" in s.error


# ---------- Real LangChain runtime test ----------


def test_real_chat_model_invoke_records_span_with_explicit_handler(sdk):
    """Use FakeMessagesListChatModel — exercises real LangChain callback dispatch
    without making an external API call."""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    handler = LuminCallbackHandler()
    llm = FakeMessagesListChatModel(responses=[AIMessage(content="Paris")])

    response = llm.invoke("What is the capital of France?", config={"callbacks": [handler]})
    assert response.content == "Paris"

    spans = _drain(sdk)
    assert len(spans) >= 1
    llm_spans = [s for s in spans if s.type == "llm"]
    assert len(llm_spans) == 1
    assert llm_spans[0].error is None
    # parent_span_id is None — no chain wrapping this
    assert llm_spans[0].parent_span_id is None


# ---------- Auto-registration via env var ----------


def test_register_configure_hook_is_registered_for_lumin_tracing():
    """The integration registers its hook at import time."""
    from langchain_core.tracers.context import _configure_hooks

    found = False
    for entry in _configure_hooks:
        # Hook entries are tuples; the env_var is one of them
        if "LUMIN_TRACING" in entry:
            found = True
            break
    assert found, "LuminCallbackHandler hook not registered for LUMIN_TRACING"


def test_lumin_tracing_env_var_attaches_handler(sdk, monkeypatch):
    """With LUMIN_TRACING=true, invoking a chain should auto-attach our handler."""
    monkeypatch.setenv("LUMIN_TRACING", "true")
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    llm = FakeMessagesListChatModel(responses=[AIMessage(content="hello")])
    response = llm.invoke("hi")
    assert response.content == "hello"

    spans = _drain(sdk)
    llm_spans = [s for s in spans if s.type == "llm"]
    assert len(llm_spans) == 1, f"expected 1 LLM span via env-var, got {len(llm_spans)}"


# ---------- Cost computation unit tests ----------


def test_compute_cost_known_model():
    # gpt-4o-mini: input 0.00015, output 0.0006 per 1K
    cost = _compute_cost("gpt-4o-mini", 1000, 1000)
    assert cost == pytest.approx(0.00075, rel=1e-3)


def test_compute_cost_versioned_model_prefix_match():
    # Versioned name should still match the base prefix
    cost = _compute_cost("gpt-4o-2024-11-20", 1000, 1000)
    assert cost is not None
    # gpt-4o (not gpt-4o-mini, since mini has its own entry)
    assert cost == pytest.approx(0.0125, rel=1e-3)


def test_compute_cost_unknown_returns_none():
    assert _compute_cost("never-heard-of-this", 100, 100) is None


def test_compute_cost_missing_tokens_returns_none():
    assert _compute_cost("gpt-4o", None, 100) is None
    assert _compute_cost("gpt-4o", 100, None) is None
    assert _compute_cost(None, 100, 100) is None


# ---------- Schema invariants ----------


def test_ext_span_to_dict_includes_all_extended_fields(sdk):
    """to_dict() must emit every field the API SpanInput accepts."""
    span = _ExtSpan(
        id="x",
        trace_id="x",
        parent_span_id=None,
        name="t",
        type="llm",
    )
    span.model = "gpt-4o"
    span.provider = "openai"
    span.tokens_input = 5
    span.tokens_output = 10
    span.cost_usd = 0.0001
    span.tool_name = "search"
    d = span.to_dict()
    for k in (
        "id", "trace_id", "parent_span_id", "name", "type",
        "input", "output", "started_at", "ended_at", "error",
        "model", "provider", "tokens_input", "tokens_output",
        "cost_usd", "tool_name",
    ):
        assert k in d, f"missing key: {k}"
    assert d["model"] == "gpt-4o"
    assert d["cost_usd"] == 0.0001
