"""Verify the LangChain integration's fallback to SDK contextvars.

When LangChain has no parent_run_id (e.g., a top-level llm.invoke()),
the LangChain handler should check the SDK's current span and nest
under it if one is active. This is what makes framework integrations
like CrewAI work end-to-end.
"""

from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")

import lumin  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, LLMResult  # noqa: E402

from lumin.integrations.langchain import LuminCallbackHandler  # noqa: E402


def _serialized():
    return {
        "id": ["langchain", "chat_models", "openai", "ChatOpenAI"],
        "kwargs": {"model_name": "gpt-4o"},
        "name": "ChatOpenAI",
    }


def _result():
    return LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="hi"))]],
        llm_output={
            "token_usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "model_name": "gpt-4o",
        },
    )


def test_llm_span_nests_under_sdk_span_when_no_langchain_parent(sdk):
    """The whole point: SDK span open → LangChain handler called with no
    parent_run_id → LLM span becomes child of SDK span."""
    handler = LuminCallbackHandler()
    run_id = uuid4()

    with lumin.span("outer_workflow", type="custom") as outer:
        handler.on_chat_model_start(
            serialized=_serialized(),
            messages=[[HumanMessage(content="hi")]],
            run_id=run_id,
            # parent_run_id intentionally omitted — this is the "top-level
            # llm.invoke from inside a Lumin context" scenario
        )
        handler.on_llm_end(_result(), run_id=run_id)

    sdk.flush()

    by_name = {s.name: s for s in sdk._exporter.spans}
    assert "outer_workflow" in by_name
    assert "ChatOpenAI" in by_name

    outer_span = by_name["outer_workflow"]
    llm_span = by_name["ChatOpenAI"]

    assert llm_span.parent_span_id == outer_span.id
    assert llm_span.trace_id == outer_span.trace_id


def test_llm_span_has_no_parent_when_no_sdk_span_either(sdk):
    """Without an SDK span and without a LangChain parent_run_id, the LLM
    span becomes its own root — same as before this fallback was added."""
    handler = LuminCallbackHandler()
    run_id = uuid4()
    handler.on_chat_model_start(
        serialized=_serialized(),
        messages=[[HumanMessage(content="hi")]],
        run_id=run_id,
    )
    handler.on_llm_end(_result(), run_id=run_id)
    sdk.flush()

    llm = next(s for s in sdk._exporter.spans if s.name == "ChatOpenAI")
    assert llm.parent_span_id is None
    assert llm.trace_id == llm.id


def test_langchain_parent_run_id_takes_precedence_over_sdk_context(sdk):
    """If LangChain HAS a parent_run_id, that wins over the SDK contextvars —
    LangChain's own hierarchy is more specific than the outer SDK span."""
    handler = LuminCallbackHandler()
    chain_id = uuid4()
    llm_id = uuid4()

    with lumin.span("outer_sdk_span") as outer:
        handler.on_chain_start(
            serialized={"name": "MyChain"},
            inputs={"q": "x"},
            run_id=chain_id,
        )
        handler.on_chat_model_start(
            serialized=_serialized(),
            messages=[[HumanMessage(content="hi")]],
            run_id=llm_id,
            parent_run_id=chain_id,  # explicit LangChain parent
        )
        handler.on_llm_end(_result(), run_id=llm_id)
        handler.on_chain_end({"out": "ok"}, run_id=chain_id)

    sdk.flush()

    by_name = {s.name: s for s in sdk._exporter.spans}
    outer_span = by_name["outer_sdk_span"]
    chain = by_name["MyChain"]
    llm = by_name["ChatOpenAI"]

    # The chain has no LangChain parent — falls back to SDK's outer
    assert chain.parent_span_id == outer_span.id
    # The LLM has a LangChain parent (the chain) — uses it
    assert llm.parent_span_id == chain.id
    # All share one trace
    assert outer_span.trace_id == chain.trace_id == llm.trace_id
