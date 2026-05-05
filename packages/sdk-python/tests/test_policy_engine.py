"""Tests for the Policy Engine — Accountability Layer Part B.

Covers the spec from the session prompt:
  - YAML loading + validation
  - span_end + trace_end condition evaluation
  - safe-eval rejection of dangerous code
  - per-policy edge cases (None-valued fields, bad conditions)
"""

from pathlib import Path

import pytest

from lumin.policy import (
    Policy,
    PolicyConfigError,
    PolicyEngine,
    PolicyViolation,
    load_policy_engine,
)
from lumin.span import Span


# --- helpers ----------------------------------------------------------------


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "lumin-policies.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _basic_span(**overrides):
    s = Span(
        id="span-1",
        trace_id="trace-1",
        parent_span_id=None,
        name="my_step",
        type="custom",
        started_at="2026-05-04T10:00:00Z",
        ended_at="2026-05-04T10:00:00.500Z",
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# --- YAML loading -----------------------------------------------------------


def test_policy_loads_from_yaml(tmp_path):
    """Valid YAML → PolicyEngine created with correct policies."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: max_cost_per_trace
    description: "Alert if single trace costs more than $0.10"
    trigger: trace_end
    condition: "trace.total_cost_usd > 0.10"
    action: flag
    severity: high
  - name: slow_llm_call
    description: "Flag if any LLM call takes more than 10 seconds"
    trigger: span_end
    condition: "span.duration_ms > 10000 and span.type == 'llm'"
    action: flag
    severity: medium
""")
    eng = PolicyEngine(f)
    assert len(eng.policies) == 2
    names = {p.name for p in eng.policies}
    assert names == {"max_cost_per_trace", "slow_llm_call"}
    cost_policy = next(p for p in eng.policies if p.name == "max_cost_per_trace")
    assert cost_policy.trigger == "trace_end"
    assert cost_policy.severity == "high"
    assert cost_policy.action == "flag"


def test_invalid_yaml_raises_error(tmp_path):
    """Missing required field → clear error message on startup."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: incomplete_policy
    trigger: span_end
    severity: high
""")  # missing condition + action
    with pytest.raises(PolicyConfigError) as exc:
        PolicyEngine(f)
    msg = str(exc.value)
    assert "incomplete_policy" in msg or "condition" in msg or "action" in msg


def test_unknown_trigger_raises(tmp_path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: bad_trigger
    trigger: on_some_random_event
    condition: "1 == 1"
    action: flag
    severity: low
""")
    with pytest.raises(PolicyConfigError) as exc:
        PolicyEngine(f)
    assert "trigger" in str(exc.value).lower()


def test_unknown_action_raises(tmp_path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: bad_action
    trigger: span_end
    condition: "1 == 1"
    action: launch_nuke
    severity: high
""")
    with pytest.raises(PolicyConfigError) as exc:
        PolicyEngine(f)
    assert "action" in str(exc.value).lower()


def test_unknown_severity_raises(tmp_path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: bad_sev
    trigger: span_end
    condition: "1 == 1"
    action: flag
    severity: spicy
""")
    with pytest.raises(PolicyConfigError):
        PolicyEngine(f)


def test_missing_file_raises(tmp_path):
    with pytest.raises(PolicyConfigError) as exc:
        PolicyEngine(tmp_path / "nope.yaml")
    assert "not found" in str(exc.value)


def test_unsupported_version_raises(tmp_path):
    f = _write(tmp_path, """
version: 99
policies: []
""")
    with pytest.raises(PolicyConfigError) as exc:
        PolicyEngine(f)
    assert "version" in str(exc.value).lower()


def test_empty_policy_file_loads_as_no_op(tmp_path):
    """An empty file is not an error — engine just has zero policies."""
    f = _write(tmp_path, "")
    eng = PolicyEngine(f)
    assert eng.policies == []
    # Evaluating against an empty engine returns no violations
    assert eng.evaluate_span(_basic_span()) == []


def test_load_policy_engine_returns_none_when_no_file_configured():
    """Helper used by SDK configure() — None policy_file → None engine."""
    assert load_policy_engine(None) is None
    assert load_policy_engine("") is None


# --- span_end evaluation ----------------------------------------------------


def test_span_condition_triggered(tmp_path):
    """span.duration_ms = 500, condition 'span.duration_ms > 100'
    → violation created."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: slow_span
    description: Slow span detector
    trigger: span_end
    condition: "span.duration_ms > 100"
    action: flag
    severity: medium
""")
    eng = PolicyEngine(f)
    span = _basic_span(
        started_at="2026-05-04T10:00:00.000Z",
        ended_at="2026-05-04T10:00:00.500Z",   # 500 ms
    )
    violations = eng.evaluate_span(span)
    assert len(violations) == 1
    v = violations[0]
    assert v.policy_name == "slow_span"
    assert v.policy_description == "Slow span detector"
    assert v.severity == "medium"
    assert v.action_taken == "flag"
    assert v.trace_id == "trace-1"
    assert v.span_id == "span-1"
    assert v.condition_text == "span.duration_ms > 100"
    # actual_value is the span.duration_ms value (best-effort)
    assert v.actual_value == "500"


def test_span_condition_not_triggered(tmp_path):
    """span.duration_ms = 50, condition 'span.duration_ms > 100' → no violation."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: slow_span
    trigger: span_end
    condition: "span.duration_ms > 100"
    action: flag
    severity: medium
""")
    eng = PolicyEngine(f)
    span = _basic_span(
        started_at="2026-05-04T10:00:00.000Z",
        ended_at="2026-05-04T10:00:00.050Z",  # 50ms
    )
    assert eng.evaluate_span(span) == []


def test_span_type_filter(tmp_path):
    """Condition 'span.type == llm' fires only on LLM spans."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: any_llm_span
    trigger: span_end
    condition: "span.type == 'llm'"
    action: flag
    severity: low
""")
    eng = PolicyEngine(f)
    llm_span = _basic_span(type="llm")
    custom_span = _basic_span(type="custom")
    assert len(eng.evaluate_span(llm_span)) == 1
    assert len(eng.evaluate_span(custom_span)) == 0


def test_len_function_on_output(tmp_path):
    """`len(str(span.output)) > N` — common pattern for big outputs."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: large_output
    trigger: span_end
    condition: "len(str(span.output)) > 100"
    action: alert
    severity: low
""")
    eng = PolicyEngine(f)
    big_span = _basic_span(output="x" * 200)
    small_span = _basic_span(output="ok")
    assert len(eng.evaluate_span(big_span)) == 1
    assert len(eng.evaluate_span(small_span)) == 0


def test_span_with_none_field_does_not_fire(tmp_path):
    """A condition that references tokens_input on a custom span (where
    tokens_input is None) should NOT fire — None > 100 is not True."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: high_token_count
    trigger: span_end
    condition: "span.tokens_input > 1000"
    action: flag
    severity: low
""")
    eng = PolicyEngine(f)
    custom_span = _basic_span()  # tokens_input not set
    assert eng.evaluate_span(custom_span) == []


def test_span_end_policies_only_fire_for_span_trigger(tmp_path):
    """span_end policies must NOT run on evaluate_trace and vice versa."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: only_span
    trigger: span_end
    condition: "1 == 1"
    action: flag
    severity: low
  - name: only_trace
    trigger: trace_end
    condition: "1 == 1"
    action: flag
    severity: low
""")
    eng = PolicyEngine(f)
    span_violations = eng.evaluate_span(_basic_span())
    assert {v.policy_name for v in span_violations} == {"only_span"}
    trace_violations = eng.evaluate_trace({"id": "t-1", "trace_id": "t-1"})
    assert {v.policy_name for v in trace_violations} == {"only_trace"}


# --- trace_end evaluation ---------------------------------------------------


def test_trace_condition_triggered(tmp_path):
    """trace.total_cost_usd = 0.50, condition '> 0.10' → violation."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: max_cost
    description: Cost guard
    trigger: trace_end
    condition: "trace.total_cost_usd > 0.10"
    action: alert
    severity: high
""")
    eng = PolicyEngine(f)
    trace = {
        "id": "trace-xyz",
        "trace_id": "trace-xyz",
        "total_cost_usd": 0.50,
        "total_tokens": 5000,
    }
    violations = eng.evaluate_trace(trace)
    assert len(violations) == 1
    v = violations[0]
    assert v.policy_name == "max_cost"
    assert v.severity == "high"
    assert v.action_taken == "alert"
    assert v.trace_id == "trace-xyz"
    assert v.span_id is None
    assert v.actual_value == "0.5"


def test_trace_span_count_threshold(tmp_path):
    """trace.span_count > N — verifies the trace namespace exposes counts."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: too_many_spans
    trigger: trace_end
    condition: "trace.span_count > 100"
    action: flag
    severity: medium
""")
    eng = PolicyEngine(f)
    big = {"id": "t1", "trace_id": "t1", "span_count": 250}
    small = {"id": "t2", "trace_id": "t2", "span_count": 5}
    assert len(eng.evaluate_trace(big)) == 1
    assert eng.evaluate_trace(small) == []


def test_trace_error_count_threshold(tmp_path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: errors_present
    trigger: trace_end
    condition: "trace.error_count > 0"
    action: flag
    severity: high
""")
    eng = PolicyEngine(f)
    assert len(eng.evaluate_trace({"id": "t1", "error_count": 3})) == 1
    assert eng.evaluate_trace({"id": "t2", "error_count": 0}) == []


# --- safe-eval security -----------------------------------------------------


def test_safe_eval_rejects_dangerous_code(tmp_path):
    """A condition trying to import os and run a shell command must NOT
    execute. simpleeval rejects unknown names + dotted attribute access
    by default — verify that's actually wired up."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: pwn
    trigger: span_end
    condition: "__import__('os').system('echo PWNED')"
    action: flag
    severity: critical
""")
    eng = PolicyEngine(f)
    # If simpleeval refused to evaluate (the desired behavior), no
    # violation will be created and no shell command will run. The
    # engine logs a warning and moves on.
    violations = eng.evaluate_span(_basic_span())
    assert violations == []


def test_safe_eval_rejects_attribute_walking(tmp_path):
    """span.__class__.__bases__ etc. — common eval-bypass pattern."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: walker
    trigger: span_end
    condition: "span.__class__.__name__ == 'Span'"
    action: flag
    severity: critical
""")
    eng = PolicyEngine(f)
    violations = eng.evaluate_span(_basic_span())
    # Either simpleeval rejects the dunder (no violation) or treats
    # __class__ as a missing field on our namespace (also no
    # violation). Either way, dunder walking must not produce True.
    assert violations == []


# --- error / robustness -----------------------------------------------------


def test_one_bad_policy_does_not_block_others(tmp_path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: bad_one
    trigger: span_end
    condition: "this is not python"
    action: flag
    severity: low
  - name: good_one
    trigger: span_end
    condition: "span.duration_ms > 0"
    action: flag
    severity: low
""")
    eng = PolicyEngine(f)
    # The good policy still fires
    violations = eng.evaluate_span(_basic_span())
    assert {v.policy_name for v in violations} == {"good_one"}


def test_evaluate_span_works_with_dict(tmp_path):
    """The API ingests spans as dicts — engine must accept them too."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: high_cost
    trigger: span_end
    condition: "span.cost_usd > 0.05"
    action: alert
    severity: high
""")
    eng = PolicyEngine(f)
    span_dict = {
        "id": "s-1",
        "trace_id": "t-1",
        "type": "llm",
        "cost_usd": 0.10,
        "started_at": "2026-05-04T10:00:00Z",
        "ended_at": "2026-05-04T10:00:01Z",
    }
    violations = eng.evaluate_span(span_dict)
    assert len(violations) == 1
    assert violations[0].actual_value == "0.1"


# --- scope.agents (Phase 3) -------------------------------------------------


def test_scope_unset_applies_to_all_agents(tmp_path: Path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: universal
    trigger: trace_end
    condition: "trace.span_count > 0"
    action: flag
    severity: low
""")
    eng = PolicyEngine(f)
    p = eng.policies[0]
    assert p.scope_agents == []
    assert p.applies_to_agent("anything") is True
    assert p.applies_to_agent(None) is True


def test_scope_filters_by_agent_name(tmp_path: Path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: scoped
    trigger: trace_end
    condition: "trace.span_count > 0"
    action: alert
    severity: high
    scope:
      agents:
        - billing_agent
        - support_agent
""")
    eng = PolicyEngine(f)
    p = eng.policies[0]
    assert p.scope_agents == ["billing_agent", "support_agent"]
    assert p.applies_to_agent("billing_agent") is True
    assert p.applies_to_agent("support_agent") is True
    assert p.applies_to_agent("evil_agent") is False
    # None blocks scoped rules — unknown identity.
    assert p.applies_to_agent(None) is False


def test_evaluate_filters_by_scope(tmp_path: Path):
    """End-to-end: engine.evaluate_trace with agent_name skips
    out-of-scope rules."""
    f = _write(tmp_path, """
version: 1
policies:
  - name: only_billing
    trigger: trace_end
    condition: "trace.span_count > 0"
    action: alert
    severity: high
    scope:
      agents: [billing]
  - name: any_agent
    trigger: trace_end
    condition: "trace.span_count > 0"
    action: flag
    severity: low
""")
    eng = PolicyEngine(f)
    trace = {"id": "t1", "trace_id": "t1", "span_count": 1}

    out_billing = eng.evaluate_trace(trace, agent_name="billing")
    names = {v.policy_name for v in out_billing}
    assert names == {"only_billing", "any_agent"}

    out_other = eng.evaluate_trace(trace, agent_name="customer_support")
    names_other = {v.policy_name for v in out_other}
    assert names_other == {"any_agent"}

    out_anon = eng.evaluate_trace(trace, agent_name=None)
    names_anon = {v.policy_name for v in out_anon}
    assert names_anon == {"any_agent"}


def test_scope_rejects_non_string_agent(tmp_path: Path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: bad_scope
    trigger: trace_end
    condition: "trace.span_count > 0"
    action: alert
    severity: low
    scope:
      agents:
        - 12345
""")
    with pytest.raises(PolicyConfigError):
        PolicyEngine(f)


def test_scope_rejects_non_dict(tmp_path: Path):
    f = _write(tmp_path, """
version: 1
policies:
  - name: malformed
    trigger: trace_end
    condition: "trace.span_count > 0"
    action: alert
    severity: low
    scope: nottadict
""")
    with pytest.raises(PolicyConfigError):
        PolicyEngine(f)

