"""Tests for the CrewAI integration (Session 6).

Most tests use stand-in Crew/Agent classes — exercises the patcher logic
without requiring crewai to be installed. The end-to-end test against
real crewai is gated by importorskip.
"""

import json

import pytest

from synaptic.integrations.crewai import instrument_crew


# --- stand-in classes that mimic the crewai API surface we patch ---


class _Task:
    def __init__(self, description: str):
        self.description = description


class _StubAgent:
    def __init__(self, role: str):
        self.role = role

    def execute_task(self, task):
        # In real crewai, this would call into LangChain LLMs.
        return f"{self.role} did: {task.description}"


class _StubCrew:
    def __init__(self, agents, tasks):
        self.agents = agents
        self.tasks = tasks

    def kickoff(self, inputs=None):
        results = []
        for agent in self.agents:
            for task in self.tasks:
                results.append(agent.execute_task(task))
        return results


@pytest.fixture
def patched_classes():
    """Provide fresh stand-in classes per test (so tests don't share state)."""

    class Agent(_StubAgent):
        pass

    class Crew(_StubCrew):
        pass

    instrument_crew(crew_cls=Crew, agent_cls=Agent)
    yield Crew, Agent


# --- tests ---


def test_kickoff_creates_root_span(sdk, patched_classes):
    Crew, Agent = patched_classes
    a = Agent(role="Researcher")
    c = Crew(agents=[a], tasks=[_Task("Research AI trends")])
    c.kickoff()
    sdk.flush()

    spans = sdk._exporter.spans
    crew_spans = [s for s in spans if s.name == "crew"]
    assert len(crew_spans) == 1
    crew_span = crew_spans[0]
    assert crew_span.parent_span_id is None
    assert crew_span.trace_id == crew_span.id
    assert crew_span.error is None


def test_agent_execute_is_child_of_crew_span(sdk, patched_classes):
    Crew, Agent = patched_classes
    researcher = Agent(role="Researcher")
    writer = Agent(role="Writer")
    c = Crew(
        agents=[researcher, writer],
        tasks=[_Task("Investigate X"), _Task("Write report")],
    )
    c.kickoff()
    sdk.flush()

    by_name = {}
    for s in sdk._exporter.spans:
        by_name.setdefault(s.name, []).append(s)

    crew = by_name["crew"][0]
    researchers = by_name["agent:Researcher"]
    writers = by_name["agent:Writer"]

    # 2 agents x 2 tasks = 4 agent spans
    assert len(researchers) + len(writers) == 4

    # Every agent span must be a child of the crew span and share its trace
    for s in researchers + writers:
        assert s.parent_span_id == crew.id, (
            f"agent span {s.name} parent={s.parent_span_id} expected={crew.id}"
        )
        assert s.trace_id == crew.trace_id


def test_crew_input_and_output_captured(sdk, patched_classes):
    Crew, Agent = patched_classes
    a = Agent(role="Researcher")
    c = Crew(agents=[a], tasks=[_Task("Research AI trends")])
    c.kickoff()
    sdk.flush()

    crew = next(s for s in sdk._exporter.spans if s.name == "crew")
    inp = json.loads(crew.input)
    assert inp["agents"] == ["Researcher"]
    assert inp["tasks"] == ["Research AI trends"]
    out = json.loads(crew.output)
    assert "Research AI trends" in out["output"]


def test_agent_input_includes_role_and_task(sdk, patched_classes):
    Crew, Agent = patched_classes
    a = Agent(role="Researcher")
    c = Crew(agents=[a], tasks=[_Task("Research AI trends")])
    c.kickoff()
    sdk.flush()

    agent_span = next(s for s in sdk._exporter.spans if s.name == "agent:Researcher")
    inp = json.loads(agent_span.input)
    assert inp["role"] == "Researcher"
    assert inp["task"] == "Research AI trends"


def test_kickoff_error_captured(sdk, patched_classes):
    Crew, Agent = patched_classes

    class BoomCrew(Crew):
        def kickoff(self, inputs=None):
            raise RuntimeError("crew failed")

    # Re-instrument the subclass — original wrapping is on Crew, but BoomCrew's
    # kickoff overrides it. Re-wrap so the new method is also traced.
    instrument_crew(crew_cls=BoomCrew, agent_cls=Agent)

    c = BoomCrew(agents=[], tasks=[])
    with pytest.raises(RuntimeError):
        c.kickoff()
    sdk.flush()

    crew = next(s for s in sdk._exporter.spans if s.name == "crew")
    assert crew.error is not None
    assert "RuntimeError" in crew.error
    assert "crew failed" in crew.error


def test_instrumentation_is_idempotent(sdk, patched_classes):
    """Calling instrument_crew twice must not double-wrap."""
    Crew, Agent = patched_classes
    instrument_crew(crew_cls=Crew, agent_cls=Agent)  # second call — no-op

    a = Agent(role="Researcher")
    c = Crew(agents=[a], tasks=[_Task("x")])
    c.kickoff()
    sdk.flush()

    crew_spans = [s for s in sdk._exporter.spans if s.name == "crew"]
    # Exactly one crew span — not two (which would happen with double-wrapping)
    assert len(crew_spans) == 1


def test_original_method_preserved_on_wrapper(patched_classes):
    Crew, _ = patched_classes
    assert getattr(Crew.kickoff, "_synaptic_wrapped", False) is True
    assert callable(getattr(Crew.kickoff, "_synaptic_original", None))


# --- LangChain interop: LLM spans nest under the crew span ---


def test_langchain_llm_within_crew_nests_under_agent_span(sdk, patched_classes):
    """When CrewAI calls a LangChain LLM, the LLM span should be a child of
    the agent span (via the LangChain integration's SDK-contextvars fallback).
    """
    pytest.importorskip("langchain_core")
    from uuid import uuid4

    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from synaptic.integrations.langchain import SynapticCallbackHandler

    Crew, Agent = patched_classes
    handler = SynapticCallbackHandler()

    class LLMAgent(Agent):
        def execute_task(self, task):
            run_id = uuid4()
            handler.on_chat_model_start(
                serialized={
                    "id": ["langchain", "chat_models", "openai", "ChatOpenAI"],
                    "kwargs": {"model_name": "gpt-4o"},
                    "name": "ChatOpenAI",
                },
                messages=[[HumanMessage(content=task.description)]],
                run_id=run_id,
            )
            handler.on_llm_end(
                LLMResult(
                    generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
                    llm_output={
                        "token_usage": {"prompt_tokens": 5, "completion_tokens": 2},
                        "model_name": "gpt-4o",
                    },
                ),
                run_id=run_id,
            )
            return "done"

    instrument_crew(crew_cls=Crew, agent_cls=LLMAgent)
    a = LLMAgent(role="Researcher")
    c = Crew(agents=[a], tasks=[_Task("Find facts")])
    c.kickoff()
    sdk.flush()

    by_name = {s.name: s for s in sdk._exporter.spans}
    crew = by_name["crew"]
    agent = by_name["agent:Researcher"]
    llm = by_name["ChatOpenAI"]

    # Hierarchy: crew → agent → LLM, all sharing the crew's trace_id
    assert agent.parent_span_id == crew.id
    assert llm.parent_span_id == agent.id
    assert llm.trace_id == agent.trace_id == crew.trace_id

    # And the LLM span still has its rich fields populated
    d = llm.to_dict()
    assert d["model"] == "gpt-4o"
    assert d["tokens_input"] == 5
    assert d["tokens_output"] == 2
    assert d["cost_usd"] is not None  # gpt-4o is in the price table
