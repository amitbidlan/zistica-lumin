"""Tests for the LlamaIndex callback integration."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core.callbacks.base_handler")

from llama_index.core.callbacks.schema import CBEventType, EventPayload  # noqa: E402

from synaptic.integrations.llama_index import (  # noqa: E402
    SynapticCallbackHandler,
    _classify,
    _compute_cost,
    _extract_model,
    _extract_retrieval_output,
    _extract_text,
    _extract_token_counts,
    _provider_from_model,
)


# ---------- helpers ----------


def _drain(sdk):
    sdk.flush()
    return sdk._exporter.spans


def _make_handler() -> SynapticCallbackHandler:
    return SynapticCallbackHandler()


def _ev() -> str:
    return str(uuid4())


# ---------- pure function tests ----------


def test_classify_known_event_types():
    assert _classify(CBEventType.LLM) == ("llm", "llm_call")
    assert _classify(CBEventType.RETRIEVE) == ("retrieval", "retrieve")
    assert _classify(CBEventType.EMBEDDING) == ("embedding", "embedding")
    assert _classify(CBEventType.QUERY) == ("custom", "query")
    assert _classify(CBEventType.FUNCTION_CALL) == ("tool", "function_call")


def test_compute_cost_known_model():
    assert _compute_cost("gpt-4o", 1000, 1000) == pytest.approx(0.0125, rel=1e-3)


def test_compute_cost_unknown_model_is_none():
    assert _compute_cost("brand-new-model", 100, 100) is None


def test_provider_from_model():
    assert _provider_from_model("gpt-4o-mini") == "openai"
    assert _provider_from_model("claude-opus-4") == "anthropic"
    assert _provider_from_model("text-embedding-3-small") == "openai"
    assert _provider_from_model("ollama/llama3") == "ollama"


def test_extract_text_handles_strings_messages_lists():
    assert _extract_text("hello") == "hello"
    msg = SimpleNamespace(role="user", content="hi")
    assert _extract_text(msg) == "user: hi"
    msgs = [
        SimpleNamespace(role="system", content="be helpful"),
        SimpleNamespace(role="user", content="2+2?"),
    ]
    assert _extract_text(msgs) == "system: be helpful\nuser: 2+2?"
    assert _extract_text(None) == ""


def test_extract_token_counts_from_response_raw_dict():
    response = SimpleNamespace(
        raw={"usage": {"prompt_tokens": 50, "completion_tokens": 12}}
    )
    payload = {EventPayload.RESPONSE: response}
    assert _extract_token_counts(payload) == (50, 12)


def test_extract_token_counts_from_response_raw_attribute():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    response = SimpleNamespace(raw=SimpleNamespace(usage=usage))
    payload = {EventPayload.RESPONSE: response}
    assert _extract_token_counts(payload) == (10, 5)


def test_extract_token_counts_falls_back_to_additional_kwargs():
    payload = {
        EventPayload.ADDITIONAL_KWARGS: {
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}
        }
    }
    assert _extract_token_counts(payload) == (7, 3)


def test_extract_token_counts_returns_none_when_absent():
    assert _extract_token_counts({}) == (None, None)
    assert _extract_token_counts(None) == (None, None)


def test_extract_model_from_payload():
    assert (
        _extract_model({EventPayload.MODEL_NAME: "gpt-4o-mini"})
        == "gpt-4o-mini"
    )
    assert (
        _extract_model(
            {EventPayload.SERIALIZED: {"model": "claude-opus-4-20250514"}}
        )
        == "claude-opus-4-20250514"
    )


def test_extract_retrieval_output_summarizes_nodes():
    nodes = [
        SimpleNamespace(get_text=lambda: "first chunk", score=0.92),
        SimpleNamespace(get_text=lambda: "second chunk", score=0.81),
    ]
    text, count, top = _extract_retrieval_output({EventPayload.NODES: nodes})
    assert "first chunk" in text and "second chunk" in text
    assert "0.920" in text
    assert count == 2
    assert top == pytest.approx(0.92)


# ---------- handler behavior ----------


def test_query_creates_trace(sdk):
    """A query that contains a retrieval and an LLM call results in a
    multi-span trace. The QUERY event itself is the root — we don't
    emit a synthetic span for start_trace to avoid a duplicate row."""
    h = _make_handler()
    h.start_trace("q1")
    qe = _ev()
    h.on_event_start(CBEventType.QUERY, {EventPayload.QUERY_STR: "what is X?"}, event_id=qe)
    re = _ev()
    h.on_event_start(
        CBEventType.RETRIEVE,
        {EventPayload.QUERY_STR: "what is X?"},
        event_id=re,
        parent_id=qe,
    )
    nodes = [SimpleNamespace(get_text=lambda: "X is a thing.", score=0.9)]
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: nodes}, event_id=re)
    le = _ev()
    h.on_event_start(
        CBEventType.LLM,
        {EventPayload.MESSAGES: [SimpleNamespace(role="user", content="explain X")]},
        event_id=le,
        parent_id=qe,
    )
    h.on_event_end(
        CBEventType.LLM,
        {
            EventPayload.RESPONSE: SimpleNamespace(
                raw={"usage": {"prompt_tokens": 12, "completion_tokens": 9}, "model": "gpt-4o"}
            ),
            EventPayload.MODEL_NAME: "gpt-4o",
        },
        event_id=le,
    )
    h.on_event_end(CBEventType.QUERY, {EventPayload.RESPONSE: "X is a thing."}, event_id=qe)
    h.end_trace("q1")

    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    # No "q1" synthetic root any more — the QUERY event is the root.
    assert "q1" not in by_name
    assert "query" in by_name
    assert "retrieve" in by_name
    assert "llm_call" in by_name
    assert len(spans) == 3

    # One trace, with 'query' as root and 'retrieve' + 'llm_call' as
    # children (we passed parent_id=qe for both)
    assert len({s.trace_id for s in spans}) == 1
    root = by_name["query"]
    assert root.parent_span_id is None
    assert by_name["retrieve"].parent_span_id == root.id
    assert by_name["llm_call"].parent_span_id == root.id


def test_retrieval_span_records_node_count_and_content(sdk):
    h = _make_handler()
    h.start_trace("t-retr")
    re = _ev()
    h.on_event_start(
        CBEventType.RETRIEVE,
        {EventPayload.QUERY_STR: "capital of France"},
        event_id=re,
    )
    nodes = [
        SimpleNamespace(get_text=lambda: "Paris is the capital of France.", score=0.95),
        SimpleNamespace(get_text=lambda: "France is a country.", score=0.72),
    ]
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: nodes}, event_id=re)
    h.end_trace("t-retr")

    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    retr = by_name["retrieve"]
    assert retr.type == "retrieval"
    assert "Paris" in retr.output
    assert "France is a country" in retr.output
    assert "0.950" in retr.output
    assert retr.input is not None
    assert "capital of France" in retr.input


def test_llm_span_has_model_tokens_and_cost(sdk):
    h = _make_handler()
    h.start_trace("t-llm")
    le = _ev()
    h.on_event_start(
        CBEventType.LLM,
        {
            EventPayload.MESSAGES: [
                SimpleNamespace(role="user", content="Hello, world.")
            ]
        },
        event_id=le,
    )
    response = SimpleNamespace(
        raw={"usage": {"prompt_tokens": 100, "completion_tokens": 50}, "model": "gpt-4o-mini"}
    )
    h.on_event_end(
        CBEventType.LLM,
        {
            EventPayload.RESPONSE: response,
            EventPayload.MODEL_NAME: "gpt-4o-mini",
        },
        event_id=le,
    )
    h.end_trace("t-llm")

    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    llm = by_name["llm_call"]
    assert llm.type == "llm"
    assert llm.model == "gpt-4o-mini"
    assert llm.provider == "openai"
    assert llm.tokens_input == 100
    assert llm.tokens_output == 50
    assert llm.cost_usd is not None and llm.cost_usd > 0


def test_llm_span_estimates_tokens_when_llm_doesnt_report(sdk):
    h = _make_handler()
    h.start_trace("t-est")
    le = _ev()
    h.on_event_start(
        CBEventType.LLM,
        {EventPayload.PROMPT: "x" * 80},
        event_id=le,
    )
    h.on_event_end(
        CBEventType.LLM,
        {
            EventPayload.COMPLETION: "y" * 200,
            EventPayload.MODEL_NAME: "gpt-4o",
        },
        event_id=le,
    )
    h.end_trace("t-est")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    # Estimate: ~4 chars per token. Should be > 0 even though no usage data.
    assert llm.tokens_input is not None and llm.tokens_input > 0
    assert llm.tokens_output is not None and llm.tokens_output > 0
    assert llm.cost_usd is not None


def test_embedding_span_captures_model_and_input(sdk):
    h = _make_handler()
    h.start_trace("t-embed")
    ee = _ev()
    h.on_event_start(
        CBEventType.EMBEDDING,
        {
            EventPayload.SERIALIZED: {"model": "text-embedding-3-small"},
            EventPayload.CHUNKS: ["hello world", "another chunk"],
        },
        event_id=ee,
    )
    h.on_event_end(
        CBEventType.EMBEDDING,
        {EventPayload.EMBEDDINGS: [[0.1, 0.2], [0.3, 0.4]]},
        event_id=ee,
    )
    h.end_trace("t-embed")
    spans = _drain(sdk)
    emb = next(s for s in spans if s.name == "embedding")
    assert emb.type == "embedding"
    assert emb.model == "text-embedding-3-small"
    assert emb.provider == "openai"
    assert emb.input is not None
    assert "hello world" in emb.input
    assert "2 embedding" in emb.output


def test_error_captured_when_normal_event_ends_with_exception_payload(sdk):
    """LlamaIndex's CallbackManager.event context manager calls
    on_event_end with payload={EXCEPTION: e} when the operation
    raises — regardless of event_type. The error must surface on
    that span, not be dropped."""
    h = _make_handler()
    h.start_trace("t-mid-fail")
    eid = _ev()
    h.on_event_start(
        CBEventType.RETRIEVE, {EventPayload.QUERY_STR: "x"}, event_id=eid
    )
    # Simulate the pipeline throwing — CallbackManager calls
    # on_event_end with payload[EXCEPTION] set
    h.on_event_end(
        CBEventType.RETRIEVE,
        {EventPayload.EXCEPTION: RuntimeError("embedder outage")},
        event_id=eid,
    )
    h.end_trace("t-mid-fail")
    spans = _drain(sdk)
    retr = next(s for s in spans if s.name == "retrieve")
    assert retr.error is not None
    assert "RuntimeError" in retr.error
    assert "embedder outage" in retr.error


def test_error_captured_via_exception_event(sdk):
    h = _make_handler()
    h.start_trace("t-err")
    ee = _ev()
    h.on_event_start(CBEventType.EXCEPTION, {}, event_id=ee)
    h.on_event_end(
        CBEventType.EXCEPTION,
        {EventPayload.EXCEPTION: ValueError("retrieval failed")},
        event_id=ee,
    )
    h.end_trace("t-err")
    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    exc = by_name["exception"]
    assert exc.error is not None
    assert "ValueError" in exc.error
    assert "retrieval failed" in exc.error


def test_function_call_span_is_tool_type(sdk):
    h = _make_handler()
    h.start_trace("t-fn")
    fe = _ev()
    tool = SimpleNamespace(metadata=SimpleNamespace(name="search_web"))
    h.on_event_start(
        CBEventType.FUNCTION_CALL,
        {
            EventPayload.TOOL: tool,
            EventPayload.FUNCTION_CALL: {"args": {"q": "weather SF"}},
        },
        event_id=fe,
    )
    h.on_event_end(
        CBEventType.FUNCTION_CALL,
        {EventPayload.FUNCTION_OUTPUT: "62°F"},
        event_id=fe,
    )
    h.end_trace("t-fn")
    spans = _drain(sdk)
    fn = next(s for s in spans if s.name == "function_call")
    assert fn.type == "tool"
    assert fn.tool_name == "search_web"
    assert "62" in fn.output


def test_unknown_event_type_does_not_crash(sdk):
    h = _make_handler()
    h.start_trace("t-misc")
    me = _ev()
    h.on_event_start(CBEventType.NODE_PARSING, {}, event_id=me)
    h.on_event_end(CBEventType.NODE_PARSING, {}, event_id=me)
    h.end_trace("t-misc")
    spans = _drain(sdk)
    assert any(s.name == "node_parsing" for s in spans)


def test_orphaned_end_does_not_crash(sdk):
    """An on_event_end without a matching on_event_start must not
    raise. LlamaIndex's CallbackManager occasionally double-fires."""
    h = _make_handler()
    h.start_trace("t-orph")
    h.on_event_end(CBEventType.LLM, {}, event_id="never-started")
    h.end_trace("t-orph")
    # No exception = pass


def test_handler_inside_synaptic_trace_links_under_outer(sdk):
    """When wrapped in @synaptic.trace, the first LlamaIndex event
    should attach under the outer @synaptic.trace span."""
    import synaptic

    @synaptic.trace
    def my_agent() -> None:
        h = _make_handler()
        h.start_trace("inner_query")
        e = _ev()
        h.on_event_start(CBEventType.QUERY, {EventPayload.QUERY_STR: "x"}, event_id=e)
        h.on_event_end(CBEventType.QUERY, {EventPayload.RESPONSE: "ok"}, event_id=e)
        h.end_trace("inner_query")

    my_agent()
    spans = _drain(sdk)
    by_name = {s.name: s for s in spans}
    outer = by_name["my_agent"]
    inner = by_name["query"]
    assert inner.trace_id == outer.trace_id
    assert inner.parent_span_id == outer.id


def test_to_dict_includes_extended_fields(sdk):
    """Spans emitted by this handler must round-trip through the
    standard exporter — i.e. include all _ExtSpan fields in to_dict."""
    h = _make_handler()
    h.start_trace("t-dict")
    le = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=le)
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.COMPLETION: "ok", EventPayload.MODEL_NAME: "gpt-4o"},
        event_id=le,
    )
    h.end_trace("t-dict")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    d = llm.to_dict()
    for k in (
        "model",
        "provider",
        "tokens_input",
        "tokens_output",
        "cost_usd",
        "tool_name",
        "span_subtype",
        "thinking_tokens",
    ):
        assert k in d


# ---------- resilience ----------


def test_agent_continues_if_synaptic_down(sdk, monkeypatch):
    """If the SDK's submit() throws (e.g. queue exploded), the
    handler must still let LlamaIndex finish. Rule 7."""
    h = _make_handler()

    def boom(*_a, **_k):
        raise RuntimeError("synaptic backend exploded")

    # Patch submit on the live sdk to simulate failure
    monkeypatch.setattr(sdk, "submit", boom)
    h.start_trace("t-down")
    le = _ev()
    h.on_event_start(CBEventType.LLM, {}, event_id=le)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=le)
    h.end_trace("t-down")
    # No exception escaped = pass


def test_handler_double_init_is_safe():
    """Constructing two handlers with default args doesn't crash and
    each gets its own state."""
    h1 = SynapticCallbackHandler()
    h2 = SynapticCallbackHandler()
    assert h1 is not h2
    assert h1._spans is not h2._spans
    assert h1._active_traces is not h2._active_traces


# ---------- zero-config ----------


def test_zero_config_attaches_when_env_set(monkeypatch):
    from llama_index.core import Settings
    from llama_index.core.callbacks import CallbackManager

    from synaptic.integrations.llama_index import _maybe_register_global

    monkeypatch.setenv("SYNAPTIC_TRACING", "true")
    Settings.callback_manager = CallbackManager([])
    assert _maybe_register_global() is True
    handlers = Settings.callback_manager.handlers
    assert any(isinstance(h, SynapticCallbackHandler) for h in handlers)


def test_zero_config_disabled_when_env_unset(monkeypatch):
    from llama_index.core import Settings
    from llama_index.core.callbacks import CallbackManager

    from synaptic.integrations.llama_index import _maybe_register_global

    monkeypatch.delenv("SYNAPTIC_TRACING", raising=False)
    Settings.callback_manager = CallbackManager([])
    assert _maybe_register_global() is False


def test_zero_config_idempotent(monkeypatch):
    from llama_index.core import Settings
    from llama_index.core.callbacks import CallbackManager

    from synaptic.integrations.llama_index import _maybe_register_global

    monkeypatch.setenv("SYNAPTIC_TRACING", "true")
    Settings.callback_manager = CallbackManager([SynapticCallbackHandler()])
    assert _maybe_register_global() is True
    handlers = Settings.callback_manager.handlers
    # Should not have added a second handler
    syn_handlers = [h for h in handlers if isinstance(h, SynapticCallbackHandler)]
    assert len(syn_handlers) == 1
