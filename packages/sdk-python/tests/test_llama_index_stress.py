"""Stress tests for the LlamaIndex callback integration.

Covers edge cases that real production traffic could hit but the
core test suite doesn't exercise: token usage on response.additional_kwargs,
deeply-nested traces, concurrent ingestion within one trace, the
real BaseQueryEngine event sequence, payloads with non-standard
shapes, and resilience under SDK failure."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core.callbacks.base_handler")

from llama_index.core.callbacks.schema import CBEventType, EventPayload  # noqa: E402

from synaptic.integrations.llama_index import (  # noqa: E402
    SynapticCallbackHandler,
    _extract_token_counts,
)


def _drain(sdk):
    sdk.flush()
    return sdk._exporter.spans


def _ev() -> str:
    return str(uuid4())


# ---------- token extraction edge cases ----------


def test_token_counts_via_response_additional_kwargs():
    """Some LLM SDKs (e.g. earlier llama-index-llms-openai) attach
    usage to response.additional_kwargs rather than response.raw."""
    response = SimpleNamespace(
        raw=None,
        additional_kwargs={"usage": {"prompt_tokens": 22, "completion_tokens": 11}},
    )
    payload = {EventPayload.RESPONSE: response}
    assert _extract_token_counts(payload) == (22, 11)


def test_token_counts_with_unusual_keys():
    """Anthropic-style integrations report input_tokens/output_tokens
    instead of prompt_tokens/completion_tokens."""
    response = SimpleNamespace(
        raw={"usage": {"input_tokens": 30, "output_tokens": 20}}
    )
    payload = {EventPayload.RESPONSE: response}
    assert _extract_token_counts(payload) == (30, 20)


def test_token_counts_zero_is_kept():
    """A response with zero prompt_tokens (fully cached input) must
    be recorded as 0, not silently dropped to None — otherwise we
    skip the cost calculation for cached prompts."""
    response = SimpleNamespace(
        raw={"usage": {"prompt_tokens": 0, "completion_tokens": 5}}
    )
    payload = {EventPayload.RESPONSE: response}
    tin, tout = _extract_token_counts(payload)
    assert tin == 0
    assert tout == 5


def test_token_counts_input_tokens_zero_anthropic_style():
    """Anthropic uses input_tokens/output_tokens. Zero must survive."""
    response = SimpleNamespace(
        raw={"usage": {"input_tokens": 0, "output_tokens": 12}}
    )
    payload = {EventPayload.RESPONSE: response}
    assert _extract_token_counts(payload) == (0, 12)


def test_embedding_cost_computed_from_input_tokens(sdk):
    """Embedding spans on a known model (text-embedding-3-small) must
    get a non-zero cost computed from tokens_input alone."""
    h = SynapticCallbackHandler()
    h.start_trace("emb")
    eid = _ev()
    h.on_event_start(
        CBEventType.EMBEDDING,
        {EventPayload.SERIALIZED: {"model": "text-embedding-3-small"}},
        event_id=eid,
    )
    h.on_event_end(
        CBEventType.EMBEDDING,
        {
            EventPayload.CHUNKS: ["a paragraph " * 200],  # ~2400 chars
            EventPayload.EMBEDDINGS: [[0.1, 0.2]],
        },
        event_id=eid,
    )
    h.end_trace("emb")
    spans = _drain(sdk)
    emb = next(s for s in spans if s.name == "embedding")
    assert emb.model == "text-embedding-3-small"
    assert emb.cost_usd is not None and emb.cost_usd > 0


def test_token_counts_falls_back_when_primary_key_missing():
    """If only input_tokens is set (no prompt_tokens), still returns
    a non-None value."""
    response = SimpleNamespace(
        raw={"usage": {"input_tokens": 7, "output_tokens": 3}}
    )
    payload = {EventPayload.RESPONSE: response}
    assert _extract_token_counts(payload) == (7, 3)


# ---------- deep nesting ----------


def test_deeply_nested_events_all_link_under_root(sdk):
    """A chain of 10 events nested inside a query should all share
    one trace_id. The first event becomes the root; later events
    chain via parent_id."""
    h = SynapticCallbackHandler()
    h.start_trace("deep")
    parent_event_id = ""
    expected_chain = []
    for i in range(10):
        eid = _ev()
        h.on_event_start(
            CBEventType.QUERY,
            {EventPayload.QUERY_STR: f"step-{i}"},
            event_id=eid,
            parent_id=parent_event_id,
        )
        expected_chain.append(eid)
        parent_event_id = eid
    for eid in reversed(expected_chain):
        h.on_event_end(
            CBEventType.QUERY,
            {EventPayload.RESPONSE: f"done-{eid[:6]}"},
            event_id=eid,
        )
    h.end_trace("deep")

    spans = _drain(sdk)
    # No synthetic root span is emitted any more — exactly 10 query
    # events, all under a single trace_id.
    assert len(spans) == 10
    assert len({s.trace_id for s in spans}) == 1
    roots = [s for s in spans if s.parent_span_id is None]
    assert len(roots) == 1


# ---------- multi-trace interleaving ----------


def test_two_concurrent_traces_are_isolated(sdk):
    """A handler may receive interleaved events from two traces.
    Each event must produce its own span."""
    h = SynapticCallbackHandler()
    h.start_trace("trace-A")
    h.start_trace("trace-B")
    a_id = _ev()
    b_id = _ev()
    h.on_event_start(
        CBEventType.RETRIEVE, {EventPayload.QUERY_STR: "qA"}, event_id=a_id
    )
    h.on_event_start(
        CBEventType.RETRIEVE, {EventPayload.QUERY_STR: "qB"}, event_id=b_id
    )
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: []}, event_id=a_id)
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: []}, event_id=b_id)
    h.end_trace("trace-A")
    h.end_trace("trace-B")

    spans = _drain(sdk)
    retrieves = [s for s in spans if s.name == "retrieve"]
    assert len(retrieves) == 2
    # Each is its own root (parent_span_id is None) since no outer
    # @synaptic.trace was active and parent_id was empty
    assert all(s.parent_span_id is None for s in retrieves)
    # And both can carry distinct query inputs
    inputs = sorted(s.input or "" for s in retrieves)
    assert any("qA" in i for i in inputs)
    assert any("qB" in i for i in inputs)


# ---------- payload defensiveness ----------


def test_llm_event_with_no_payload_at_all(sdk):
    """LlamaIndex sometimes calls on_event_end with payload=None."""
    h = SynapticCallbackHandler()
    h.start_trace("t-none")
    e = _ev()
    h.on_event_start(CBEventType.LLM, None, event_id=e)
    h.on_event_end(CBEventType.LLM, None, event_id=e)
    h.end_trace("t-none")
    spans = _drain(sdk)
    # Should still produce the LLM span, just with no input/output
    assert any(s.name == "llm_call" for s in spans)


def test_retrieve_with_node_get_text_raising_does_not_crash(sdk):
    """A weird node whose get_text() raises must not break the trace."""
    h = SynapticCallbackHandler()
    h.start_trace("t-bad-node")
    re = _ev()
    h.on_event_start(CBEventType.RETRIEVE, {EventPayload.QUERY_STR: "q"}, event_id=re)

    class BadNode:
        def get_text(self):
            raise RuntimeError("text not loaded")

        score = 0.5

    h.on_event_end(
        CBEventType.RETRIEVE,
        {EventPayload.NODES: [BadNode()]},
        event_id=re,
    )
    h.end_trace("t-bad-node")
    spans = _drain(sdk)
    # Span still emitted — bad node is recorded as best-effort str()
    assert any(s.name == "retrieve" for s in spans)


def test_llm_with_unknown_model_records_no_cost(sdk):
    h = SynapticCallbackHandler()
    h.start_trace("t-unk-model")
    e = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=e)
    response = SimpleNamespace(
        raw={"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "future-model"},
    )
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.RESPONSE: response, EventPayload.MODEL_NAME: "future-model"},
        event_id=e,
    )
    h.end_trace("t-unk-model")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.model == "future-model"
    assert llm.cost_usd is None


# ---------- realistic event order from a query engine ----------


def test_realistic_query_engine_event_order(sdk):
    """Mirrors the actual order LlamaIndex emits for a vector
    query_engine.query() call: QUERY → RETRIEVE → EMBEDDING (inside)
    → SYNTHESIZE → CHUNKING / TEMPLATING / LLM. All under one root."""
    h = SynapticCallbackHandler()
    h.start_trace("query")
    q = _ev()
    h.on_event_start(
        CBEventType.QUERY, {EventPayload.QUERY_STR: "what is X?"}, event_id=q
    )
    r = _ev()
    h.on_event_start(
        CBEventType.RETRIEVE,
        {EventPayload.QUERY_STR: "what is X?"},
        event_id=r,
        parent_id=q,
    )
    eb = _ev()
    h.on_event_start(
        CBEventType.EMBEDDING,
        {EventPayload.SERIALIZED: {"model": "text-embedding-3-small"}},
        event_id=eb,
        parent_id=r,
    )
    h.on_event_end(
        CBEventType.EMBEDDING,
        {EventPayload.EMBEDDINGS: [[0.0] * 4]},
        event_id=eb,
    )
    nodes = [SimpleNamespace(get_text=lambda: "X is Y", score=0.9)]
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: nodes}, event_id=r)
    s = _ev()
    h.on_event_start(CBEventType.SYNTHESIZE, {}, event_id=s, parent_id=q)
    le = _ev()
    h.on_event_start(
        CBEventType.LLM,
        {EventPayload.PROMPT: "given X is Y, answer..."},
        event_id=le,
        parent_id=s,
    )
    h.on_event_end(
        CBEventType.LLM,
        {
            EventPayload.COMPLETION: "X is Y because reasons.",
            EventPayload.MODEL_NAME: "gpt-4o-mini",
        },
        event_id=le,
    )
    h.on_event_end(CBEventType.SYNTHESIZE, {}, event_id=s)
    h.on_event_end(
        CBEventType.QUERY,
        {EventPayload.RESPONSE: "X is Y because reasons."},
        event_id=q,
    )
    h.end_trace("query")

    spans = _drain(sdk)
    by = {}
    for sp in spans:
        by.setdefault(sp.name, []).append(sp)
    # Topology: query (root) > {retrieve > embedding,
    #                           synthesize > llm_call}
    assert len(by["query"]) == 1
    root = by["query"][0]
    assert root.parent_span_id is None
    assert all(s.trace_id == root.trace_id for s in spans)
    llm = by["llm_call"][0]
    syn = by["synthesize"][0]
    retr = by["retrieve"][0]
    assert llm.parent_span_id == syn.id
    assert syn.parent_span_id == root.id
    assert retr.parent_span_id == root.id


# ---------- resilience ----------


def test_embedding_input_captured_from_end_payload_when_start_was_empty(sdk):
    """LlamaIndex's actual event sequence puts the embedded text in
    the END payload's CHUNKS field, not the start. Earlier versions
    of this handler missed it because they only looked at start."""
    h = SynapticCallbackHandler()
    h.start_trace("t-emb-end")
    e = _ev()
    # Start payload has only SERIALIZED (model info) — no chunks
    h.on_event_start(
        CBEventType.EMBEDDING,
        {EventPayload.SERIALIZED: {"model": "text-embedding-3-small"}},
        event_id=e,
    )
    # End payload includes CHUNKS
    h.on_event_end(
        CBEventType.EMBEDDING,
        {
            EventPayload.CHUNKS: ["capital of France?"],
            EventPayload.EMBEDDINGS: [[0.1, 0.2]],
        },
        event_id=e,
    )
    h.end_trace("t-emb-end")
    spans = _drain(sdk)
    emb = next(s for s in spans if s.name == "embedding")
    assert emb.input is not None
    assert "capital of France" in emb.input
    assert emb.tokens_input is not None and emb.tokens_input > 0


# ---------- rarer event types ----------


def test_agent_step_and_sub_question_and_reranking(sdk):
    """Cover the agent / sub-question / reranking event types — they
    should all classify as type=custom and not crash."""
    h = SynapticCallbackHandler()
    h.start_trace("rare")
    for ev_type, expected_name in [
        (CBEventType.AGENT_STEP, "agent_step"),
        (CBEventType.SUB_QUESTION, "sub_question"),
        (CBEventType.RERANKING, "reranking"),
        (CBEventType.TREE, "tree"),
    ]:
        eid = _ev()
        h.on_event_start(ev_type, {}, event_id=eid)
        h.on_event_end(ev_type, {}, event_id=eid)
    h.end_trace("rare")
    spans = _drain(sdk)
    names = {s.name for s in spans}
    assert {"agent_step", "sub_question", "reranking", "tree"}.issubset(names)
    for s in spans:
        assert s.type == "custom"


def test_exception_event_with_string_error(sdk):
    """Some integrations pass string errors instead of Exception
    objects via the EXCEPTION payload."""
    h = SynapticCallbackHandler()
    h.start_trace("err")
    eid = _ev()
    h.on_event_start(CBEventType.EXCEPTION, {}, event_id=eid)
    # Pass a non-Exception value
    h.on_event_end(
        CBEventType.EXCEPTION, {EventPayload.EXCEPTION: "stringified error"}, event_id=eid
    )
    h.end_trace("err")
    spans = _drain(sdk)
    exc = next(s for s in spans if s.name == "exception")
    # error should be set even though the payload was a string
    assert exc.error is not None
    assert "stringified error" in exc.error


def test_no_start_trace_events_still_emit_spans(sdk):
    """Some users may use the handler outside the trace lifecycle —
    on_event_start without start_trace must still produce a span."""
    h = SynapticCallbackHandler()
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=eid)
    h.on_event_end(
        CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=eid
    )
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.parent_span_id is None  # becomes a root span


def test_end_trace_for_never_started_trace_does_not_crash(sdk):
    """Defensive: pop on missing key shouldn't blow up."""
    h = SynapticCallbackHandler()
    h.end_trace("never-started")  # should be a no-op
    h.end_trace(None)
    # No exception = pass


def test_repeated_start_trace_with_same_id_overwrites_safely(sdk):
    """Starting the same trace_id twice without an end in between
    should not crash. The second start replaces the first's outer
    snapshot — that's acceptable since LlamaIndex shouldn't fire
    two starts for one trace_id under normal conditions."""
    h = SynapticCallbackHandler()
    h.start_trace("dup")
    h.start_trace("dup")
    eid = _ev()
    h.on_event_start(CBEventType.QUERY, {EventPayload.QUERY_STR: "x"}, event_id=eid)
    h.on_event_end(CBEventType.QUERY, {EventPayload.RESPONSE: "ok"}, event_id=eid)
    h.end_trace("dup")
    h.end_trace("dup")  # second end is a no-op
    spans = _drain(sdk)
    assert any(s.name == "query" for s in spans)


def test_chinese_and_emoji_in_query_text(sdk):
    """Unicode in inputs — Synaptic uses ensure_ascii=False so chars
    survive end-to-end."""
    h = SynapticCallbackHandler()
    h.start_trace("u")
    eid = _ev()
    h.on_event_start(
        CBEventType.RETRIEVE,
        {EventPayload.QUERY_STR: "なぜ 2+2=4? 🧠"},
        event_id=eid,
    )
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: []}, event_id=eid)
    h.end_trace("u")
    spans = _drain(sdk)
    retr = next(s for s in spans if s.name == "retrieve")
    assert retr.input is not None
    assert "なぜ" in retr.input
    assert "🧠" in retr.input


def test_extract_text_chat_response_with_nested_message(sdk):
    """Real LlamaIndex ChatResponse has .message.content — must
    surface the content cleanly, not the Python repr."""
    h = SynapticCallbackHandler()
    h.start_trace("chat-real")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=eid)
    response = SimpleNamespace(
        message=SimpleNamespace(role="assistant", content="hello there"),
        raw={"usage": {"prompt_tokens": 5, "completion_tokens": 3}},
    )
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.RESPONSE: response, EventPayload.MODEL_NAME: "gpt-4o"},
        event_id=eid,
    )
    h.end_trace("chat-real")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    # Output should be the role+content string, not "namespace(message=...)"
    assert "hello there" in (llm.output or "")
    assert "namespace" not in (llm.output or "").lower()


def test_extract_text_completion_response_with_text(sdk):
    """CompletionResponse has .text directly."""
    h = SynapticCallbackHandler()
    h.start_trace("comp")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=eid)
    response = SimpleNamespace(text="bare completion text", raw={})
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.RESPONSE: response, EventPayload.MODEL_NAME: "gpt-4o"},
        event_id=eid,
    )
    h.end_trace("comp")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert "bare completion text" in (llm.output or "")


def test_extract_text_dict_with_role_and_content(sdk):
    """Some integrations pass {role, content} dicts. Must extract
    cleanly without dumping Python repr."""
    h = SynapticCallbackHandler()
    h.start_trace("dict-msg")
    eid = _ev()
    msgs = [{"role": "system", "content": "be helpful"}, {"role": "user", "content": "hi"}]
    h.on_event_start(CBEventType.LLM, {EventPayload.MESSAGES: msgs}, event_id=eid)
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.COMPLETION: "ok", EventPayload.MODEL_NAME: "gpt-4o"},
        event_id=eid,
    )
    h.end_trace("dict-msg")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    inp = llm.input or ""
    assert "system: be helpful" in inp
    assert "user: hi" in inp


def test_response_with_message_object_extracts_content(sdk):
    """LLM responses sometimes come as ChatMessage-like objects with
    .content. Extraction should grab the content text."""
    h = SynapticCallbackHandler()
    h.start_trace("msg")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=eid)
    response = SimpleNamespace(
        message=SimpleNamespace(role="assistant", content="hello there"),
        raw={"usage": {"prompt_tokens": 5, "completion_tokens": 3}},
    )
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.RESPONSE: response, EventPayload.MODEL_NAME: "gpt-4o"},
        event_id=eid,
    )
    h.end_trace("msg")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    # _extract_text walks .content — should find "hello there"
    assert llm.output is not None
    assert "hello there" in llm.output


def test_function_call_with_simple_tool_dot_name(sdk):
    """A tool that has .name directly (older style)."""
    h = SynapticCallbackHandler()
    h.start_trace("fn-direct")
    eid = _ev()
    tool = SimpleNamespace(name="search")
    h.on_event_start(
        CBEventType.FUNCTION_CALL,
        {EventPayload.TOOL: tool, EventPayload.FUNCTION_CALL: {"q": "x"}},
        event_id=eid,
    )
    h.on_event_end(
        CBEventType.FUNCTION_CALL,
        {EventPayload.FUNCTION_OUTPUT: "result"},
        event_id=eid,
    )
    h.end_trace("fn-direct")
    spans = _drain(sdk)
    fn = next(s for s in spans if s.name == "function_call")
    assert fn.tool_name == "search"


def test_leaked_spans_are_reaped_in_end_trace_via_trace_map(sdk):
    """A start without an end (exception unwound the stack) leaves
    a span in self._spans. end_trace's trace_map tells us which
    event_ids belong to this trace; we reap those on close."""
    h = SynapticCallbackHandler()
    h.start_trace("leak")
    e1 = "leaked-evt-1"
    e2 = "leaked-evt-2"
    e3 = "closed-evt"
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "p"}, event_id=e1)
    h.on_event_start(CBEventType.RETRIEVE, {EventPayload.QUERY_STR: "q"}, event_id=e2)
    h.on_event_start(CBEventType.QUERY, {}, event_id=e3, parent_id=e1)
    h.on_event_end(CBEventType.QUERY, {EventPayload.RESPONSE: "ok"}, event_id=e3)
    # e1 and e2 never get on_event_end — simulate exception bypass.
    # trace_map records the event tree.
    h.end_trace("leak", trace_map={e1: [e3], e2: []})
    # _spans should be empty after reap
    assert len(h._spans) == 0
    spans = _drain(sdk)
    by_name = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)
    # llm_call (e1) and retrieve (e2) reaped with error message
    leaked_llm = by_name.get("llm_call", [])
    leaked_retrieve = by_name.get("retrieve", [])
    assert len(leaked_llm) == 1
    assert len(leaked_retrieve) == 1
    assert "did not receive on_event_end" in (leaked_llm[0].error or "")
    assert "did not receive on_event_end" in (leaked_retrieve[0].error or "")


def test_leaked_spans_with_no_trace_map_are_kept(sdk):
    """Defensive: if trace_map is None (older LlamaIndex versions
    that don't pass it), don't reap — we can't tell which spans
    belong to which active trace."""
    h = SynapticCallbackHandler()
    h.start_trace("no-map")
    h.on_event_start(CBEventType.LLM, {}, event_id="orphan-1")
    h.end_trace("no-map", trace_map=None)
    # Span stays in _spans (may belong to a concurrent trace)
    assert "orphan-1" in h._spans


def test_fine_tuned_openai_model_cost_resolves(sdk):
    """Real production fine-tuned model names look like
    ``ft:gpt-4o:my-org::abc123``. Cost should resolve via the base
    model's price."""
    h = SynapticCallbackHandler()
    h.start_trace("ft")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=eid)
    response = SimpleNamespace(
        raw={"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    )
    h.on_event_end(
        CBEventType.LLM,
        {
            EventPayload.RESPONSE: response,
            EventPayload.MODEL_NAME: "ft:gpt-4o:my-org::abc123",
        },
        event_id=eid,
    )
    h.end_trace("ft")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.cost_usd is not None and llm.cost_usd > 0


def test_provider_prefixed_model_cost_resolves(sdk):
    """Routers like LiteLLM / OpenRouter pass models as
    ``openai/gpt-4o-mini`` — strip the provider prefix for matching."""
    h = SynapticCallbackHandler()
    h.start_trace("router")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "hi"}, event_id=eid)
    response = SimpleNamespace(
        raw={"usage": {"prompt_tokens": 50, "completion_tokens": 10}}
    )
    h.on_event_end(
        CBEventType.LLM,
        {
            EventPayload.RESPONSE: response,
            EventPayload.MODEL_NAME: "openai/gpt-4o-mini",
        },
        event_id=eid,
    )
    h.end_trace("router")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.cost_usd is not None and llm.cost_usd > 0


def test_register_custom_model_price(sdk):
    """Users running self-hosted models can register a custom price
    so cost surfaces correctly."""
    from synaptic.integrations.llama_index import register_model_price, PRICES_PER_1K

    original = dict(PRICES_PER_1K)
    try:
        register_model_price("my-self-hosted-llama", 0.0001, 0.0002)
        h = SynapticCallbackHandler()
        h.start_trace("self")
        eid = _ev()
        h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=eid)
        response = SimpleNamespace(
            raw={"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        )
        h.on_event_end(
            CBEventType.LLM,
            {
                EventPayload.RESPONSE: response,
                EventPayload.MODEL_NAME: "my-self-hosted-llama-7b",
            },
            event_id=eid,
        )
        h.end_trace("self")
        spans = _drain(sdk)
        llm = next(s for s in spans if s.name == "llm_call")
        assert llm.cost_usd is not None
        # 100 * 0.0001/1000 + 50 * 0.0002/1000 = 1e-5 + 1e-5 = 2e-5
        assert llm.cost_usd == pytest.approx(0.00002, rel=1e-3)
    finally:
        # Restore so other tests aren't affected
        PRICES_PER_1K.clear()
        PRICES_PER_1K.update(original)


def test_zero_config_uses_add_handler_to_preserve_callback_manager(monkeypatch):
    """auto-registration must call existing.add_handler (preserving
    subclass identity / state), not replace the manager wholesale."""
    from llama_index.core import Settings
    from llama_index.core.callbacks import CallbackManager
    from synaptic.integrations.llama_index import _maybe_register_global

    class TaggedCallbackManager(CallbackManager):
        """User subclass with extra state we don't want to lose."""
        def __init__(self, handlers=None):
            super().__init__(handlers or [])
            self.tag = "user-special"

    monkeypatch.setenv("SYNAPTIC_TRACING", "true")
    user_cm = TaggedCallbackManager([])
    Settings.callback_manager = user_cm

    assert _maybe_register_global() is True
    # Same instance — not replaced
    assert Settings.callback_manager is user_cm
    assert Settings.callback_manager.tag == "user-special"
    # And our handler was added
    handlers = Settings.callback_manager.handlers
    assert any(isinstance(h, SynapticCallbackHandler) for h in handlers)


# ---------- payload pathologies ----------


def test_payload_with_bytes_value_does_not_crash(sdk):
    """Some custom integrations stash bytes in payloads. Don't crash."""
    h = SynapticCallbackHandler()
    h.start_trace("bytes")
    eid = _ev()
    h.on_event_start(
        CBEventType.LLM,
        {EventPayload.PROMPT: b"\x00\x01binary"},
        event_id=eid,
    )
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.COMPLETION: b"\xff binary out"},
        event_id=eid,
    )
    h.end_trace("bytes")
    spans = _drain(sdk)
    assert any(s.name == "llm_call" for s in spans)


def test_payload_with_circular_reference_does_not_recurse(sdk):
    """A self-referential payload (rare but possible from custom
    code) shouldn't crash _serialize via infinite recursion."""
    h = SynapticCallbackHandler()
    h.start_trace("circ")
    eid = _ev()
    bad: dict = {"key": "value"}
    bad["self"] = bad  # circular
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: bad}, event_id=eid)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=eid)
    h.end_trace("circ")
    spans = _drain(sdk)
    # No crash = pass; output may be truncated str repr
    assert any(s.name == "llm_call" for s in spans)


def test_extract_text_handles_deeply_nested_lists(sdk):
    """100-deep nested list of strings shouldn't blow the stack."""
    h = SynapticCallbackHandler()
    h.start_trace("deep-list")
    eid = _ev()
    nested: object = "leaf"
    for _ in range(100):
        nested = [nested]
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: nested}, event_id=eid)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=eid)
    h.end_trace("deep-list")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    # Even truncated, the leaf token should make it in
    assert "leaf" in (llm.input or "")


# ---------- pydantic response.raw shape ----------


def test_response_raw_is_pydantic_like_object(sdk):
    """Some LLM integrations make response.raw a typed object (not
    a dict). The fallback `getattr(raw, 'usage', None)` path must
    pick up usage data."""
    h = SynapticCallbackHandler()
    h.start_trace("pyd")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=eid)
    # Simulate a Pydantic-style raw with .usage attribute
    usage_obj = SimpleNamespace(prompt_tokens=42, completion_tokens=11)
    raw_obj = SimpleNamespace(usage=usage_obj, model="gpt-4o")
    response = SimpleNamespace(raw=raw_obj)
    h.on_event_end(
        CBEventType.LLM,
        {EventPayload.RESPONSE: response, EventPayload.MODEL_NAME: "gpt-4o"},
        event_id=eid,
    )
    h.end_trace("pyd")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.tokens_input == 42
    assert llm.tokens_output == 11
    assert llm.cost_usd is not None and llm.cost_usd > 0


# ---------- multi-handler coexistence ----------


def test_handler_coexists_with_another_callback_handler(sdk):
    """When a CallbackManager has multiple handlers (e.g. user has
    LlamaDebugHandler + Synaptic), our handler must work alongside
    without interfering."""
    from llama_index.core.callbacks.base_handler import BaseCallbackHandler
    other_calls: list = []

    class CountingHandler(BaseCallbackHandler):
        def __init__(self): super().__init__([], [])
        def on_event_start(self, event_type, payload=None, event_id="", parent_id="", **kw):
            other_calls.append(("start", event_type, event_id))
            return event_id
        def on_event_end(self, event_type, payload=None, event_id="", **kw):
            other_calls.append(("end", event_type, event_id))
        def start_trace(self, trace_id=None): other_calls.append(("st", trace_id))
        def end_trace(self, trace_id=None, trace_map=None): other_calls.append(("et", trace_id))

    h = SynapticCallbackHandler()
    other = CountingHandler()
    # Simulate CallbackManager dispatching to both — both get every callback
    for handler in (h, other):
        handler.start_trace("multi")
    eid = _ev()
    for handler in (h, other):
        handler.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=eid)
    for handler in (h, other):
        handler.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=eid)
    for handler in (h, other):
        handler.end_trace("multi")

    # Synaptic span emitted
    spans = _drain(sdk)
    assert any(s.name == "llm_call" for s in spans)
    # Other handler also saw all callbacks
    assert ("start", CBEventType.LLM, eid) in other_calls
    assert ("end", CBEventType.LLM, eid) in other_calls


# ---------- partial submit failure ----------


def test_one_failed_submit_doesnt_lose_subsequent_spans(sdk, monkeypatch):
    """If submit() fails for one span, later spans must still ship."""
    h = SynapticCallbackHandler()
    h.start_trace("partial-fail")

    real_submit = sdk.submit
    fail_once = {"count": 0}

    def submit_with_one_failure(span):
        if span.name == "embedding":
            fail_once["count"] += 1
            raise RuntimeError("simulated transient failure")
        return real_submit(span)

    monkeypatch.setattr(sdk, "submit", submit_with_one_failure)

    e1 = _ev()
    h.on_event_start(CBEventType.EMBEDDING, {EventPayload.SERIALIZED: {"model": "x"}}, event_id=e1)
    h.on_event_end(CBEventType.EMBEDDING, {EventPayload.EMBEDDINGS: [[0.1]]}, event_id=e1)

    e2 = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=e2)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=e2)

    h.end_trace("partial-fail")

    monkeypatch.setattr(sdk, "submit", real_submit)
    spans = _drain(sdk)
    # Embedding submit raised, but llm_call must still land
    assert fail_once["count"] >= 1
    assert any(s.name == "llm_call" for s in spans)


# ---------- session_id propagation through a trace ----------


def test_session_id_propagates_from_synaptic_session_through_llamaindex(sdk):
    """When an LlamaIndex query runs inside a synaptic.session(),
    every emitted span should carry the session_id."""
    import synaptic

    h = SynapticCallbackHandler()
    with synaptic.session(id="session-abc"):
        @synaptic.trace
        def run() -> None:
            h.start_trace("inside")
            eid = _ev()
            h.on_event_start(CBEventType.QUERY, {EventPayload.QUERY_STR: "x"}, event_id=eid)
            sub = _ev()
            h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=sub, parent_id=eid)
            h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=sub)
            h.on_event_end(CBEventType.QUERY, {EventPayload.RESPONSE: "ok"}, event_id=eid)
            h.end_trace("inside")
        run()

    spans = _drain(sdk)
    # All spans (outer + LlamaIndex children) tagged with session_id
    for s in spans:
        assert s.session_id == "session-abc", f"{s.name} has session_id={s.session_id}"


# ---------- end_trace with trace_map for already-closed events ----------


def test_child_spans_are_temporally_nested_inside_parents(sdk):
    """Every child's started_at must be >= parent's started_at, and
    every child's ended_at must be <= parent's ended_at. This is a
    fundamental invariant of nested spans — if violated, a timeline
    visualization shows children outside their parent's bracket."""
    h = SynapticCallbackHandler()
    h.start_trace("nest")
    parent = _ev()
    h.on_event_start(CBEventType.QUERY, {EventPayload.QUERY_STR: "x"}, event_id=parent)
    # Two children
    c1 = _ev()
    h.on_event_start(CBEventType.RETRIEVE, {EventPayload.QUERY_STR: "x"}, event_id=c1, parent_id=parent)
    h.on_event_end(CBEventType.RETRIEVE, {EventPayload.NODES: []}, event_id=c1)
    c2 = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=c2, parent_id=parent)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=c2)
    h.on_event_end(CBEventType.QUERY, {EventPayload.RESPONSE: "ok"}, event_id=parent)
    h.end_trace("nest")

    spans = _drain(sdk)
    by_id = {s.id: s for s in spans}
    for s in spans:
        if not s.parent_span_id:
            continue
        p = by_id.get(s.parent_span_id)
        if p is None:
            continue
        assert s.started_at >= p.started_at, (
            f"{s.name}.started_at={s.started_at} < parent.started_at={p.started_at}"
        )
        assert s.ended_at is not None
        assert p.ended_at is not None
        assert s.ended_at <= p.ended_at, (
            f"{s.name}.ended_at={s.ended_at} > parent.ended_at={p.ended_at}"
        )


def test_extract_model_falls_back_to_response_model_attribute(sdk):
    """When no MODEL_NAME or SERIALIZED is in payload, the model
    name lives on response.model (some integrations) — must use it."""
    h = SynapticCallbackHandler()
    h.start_trace("model-on-resp")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=eid)
    response = SimpleNamespace(
        model="gpt-4o-mini",
        raw={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    )
    # Note: deliberately NO MODEL_NAME in payload
    h.on_event_end(CBEventType.LLM, {EventPayload.RESPONSE: response}, event_id=eid)
    h.end_trace("model-on-resp")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.model == "gpt-4o-mini"
    assert llm.cost_usd is not None and llm.cost_usd > 0


def test_extract_model_falls_back_to_response_raw_dict_model(sdk):
    """Some LiteLLM-style responses put model only in response.raw['model']."""
    h = SynapticCallbackHandler()
    h.start_trace("model-in-raw")
    eid = _ev()
    h.on_event_start(CBEventType.LLM, {EventPayload.PROMPT: "x"}, event_id=eid)
    response = SimpleNamespace(
        raw={
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "model": "gpt-4o",
        },
    )
    # No MODEL_NAME, no .model attr — only raw["model"]
    h.on_event_end(CBEventType.LLM, {EventPayload.RESPONSE: response}, event_id=eid)
    h.end_trace("model-in-raw")
    spans = _drain(sdk)
    llm = next(s for s in spans if s.name == "llm_call")
    assert llm.model == "gpt-4o"
    assert llm.cost_usd is not None and llm.cost_usd > 0


def test_end_trace_trace_map_with_already_closed_events_is_safe(sdk):
    """trace_map references events that completed cleanly — reaping
    must skip them without error."""
    h = SynapticCallbackHandler()
    h.start_trace("normal")
    e1 = _ev()
    h.on_event_start(CBEventType.LLM, {}, event_id=e1)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=e1)
    # All events closed cleanly. trace_map references them.
    h.end_trace("normal", trace_map={e1: []})
    spans = _drain(sdk)
    # Exactly one llm_call span (no duplicate from reaping)
    llm_spans = [s for s in spans if s.name == "llm_call"]
    assert len(llm_spans) == 1
    assert llm_spans[0].error is None  # clean completion, no leak error


def test_handler_unaffected_by_sdk_get_failure(sdk, monkeypatch):
    """If even _get_sdk() raises, the agent must not crash."""
    h = SynapticCallbackHandler()

    import synaptic.integrations.llama_index as li_mod

    def boom():
        raise RuntimeError("sdk init failed")

    monkeypatch.setattr(li_mod, "_get_sdk", boom)
    h.start_trace("t-broken")
    e = _ev()
    h.on_event_start(CBEventType.LLM, {}, event_id=e)
    h.on_event_end(CBEventType.LLM, {EventPayload.COMPLETION: "ok"}, event_id=e)
    h.end_trace("t-broken")
    # No exception escaped
