"""CrewAI integration for Synaptic.

Call ``instrument_crew()`` once at startup; from then on every
``Crew.kickoff()`` is recorded as a root span and every
``Agent.execute_task()`` becomes a child span. LLM calls within the crew
are picked up by the LangChain integration (CrewAI uses LangChain
internally) and nest correctly under the agent span via the SDK's
contextvars fallback.

    from synaptic.integrations.crewai import instrument_crew
    instrument_crew()

    from crewai import Agent, Task, Crew
    crew = Crew(agents=[...], tasks=[...])
    crew.kickoff()
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional

from synaptic.sdk import span as _open_span


def _instrument_method(
    cls: type,
    method_name: str,
    *,
    name_fn: Callable[..., str],
    type: str = "custom",
    input_fn: Optional[Callable[..., Any]] = None,
    output_fn: Optional[Callable[..., Any]] = None,
) -> None:
    """Wrap ``cls.method_name`` so each call records a Synaptic span.

    Idempotent: if already wrapped, this is a no-op. The original method is
    preserved on the wrapper as ``_synaptic_original`` for unwrapping in tests.
    """
    original = getattr(cls, method_name, None)
    if original is None:
        raise AttributeError(f"{cls.__name__!r} has no attribute {method_name!r}")
    if getattr(original, "_synaptic_wrapped", False):
        return

    span_type = type  # avoid shadowing the builtin inside the closure

    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        try:
            span_name = name_fn(self, *args, **kwargs)
        except Exception:
            span_name = method_name

        with _open_span(name=span_name, type=span_type) as s:
            if input_fn is not None:
                try:
                    s.set_input(input_fn(self, *args, **kwargs))
                except Exception:
                    pass
            try:
                result = original(self, *args, **kwargs)
            except Exception as e:
                s.set_error(e)
                raise
            if output_fn is not None:
                try:
                    s.set_output(output_fn(self, result, *args, **kwargs))
                except Exception:
                    pass
            return result

    wrapped._synaptic_wrapped = True  # type: ignore[attr-defined]
    wrapped._synaptic_original = original  # type: ignore[attr-defined]
    setattr(cls, method_name, wrapped)


# --- name / payload extractors ---


def _crew_name(crew, *a, **kw) -> str:
    return "crew"


def _crew_input(crew, *a, **kw) -> dict:
    return {
        "agents": [getattr(ag, "role", "?") for ag in getattr(crew, "agents", [])],
        "tasks": [
            str(getattr(t, "description", t))
            for t in getattr(crew, "tasks", [])
        ],
    }


def _crew_output(crew, result, *a, **kw) -> dict:
    return {"output": str(result)[:2000]}


def _agent_name(agent, *a, **kw) -> str:
    role = getattr(agent, "role", None) or "agent"
    return f"agent:{role}"


def _agent_input(agent, task=None, *a, **kw) -> dict:
    return {
        "role": getattr(agent, "role", None),
        "task": str(getattr(task, "description", task)) if task is not None else None,
    }


def _agent_output(agent, result, *a, **kw) -> dict:
    return {"output": str(result)[:2000]}


# --- public API ---


def instrument_crew(crew_cls: Optional[type] = None, agent_cls: Optional[type] = None) -> None:
    """Monkey-patch CrewAI's ``Crew`` and ``Agent`` so each kickoff and each
    agent task execution is recorded as a Synaptic span.

    Idempotent — calling multiple times is safe.

    For tests, ``crew_cls``/``agent_cls`` can be passed explicitly to wrap
    stand-in classes without importing crewai.
    """
    if crew_cls is None or agent_cls is None:
        try:
            from crewai import Agent, Crew
        except ImportError as e:
            raise ImportError(
                "synaptic.integrations.crewai requires crewai. "
                "Install with: pip install crewai"
            ) from e
        crew_cls = crew_cls or Crew
        agent_cls = agent_cls or Agent

    _instrument_method(
        crew_cls,
        "kickoff",
        name_fn=_crew_name,
        type="custom",
        input_fn=_crew_input,
        output_fn=_crew_output,
    )
    _instrument_method(
        agent_cls,
        "execute_task",
        name_fn=_agent_name,
        type="custom",
        input_fn=_agent_input,
        output_fn=_agent_output,
    )


__all__ = ["instrument_crew"]
