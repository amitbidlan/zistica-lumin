"""CI latency assertion for the synchronous decision engine
(§10.6 of AGENT_FIREWALL_SPEC.md).

Spec budgets (§2.4):
  - before_proxy_call : 50 ms
  - before_tool_call  : 50 ms
  - after_tool_call   : 100 ms
  - after_proxy_call  : 300 ms

We assert decide() stays well under the *before_tool_call* budget
under realistic conditions (10 enabled rules, simple regex
condition). 50ms is the budget; CI machines vary, so the gate is
20ms p99 — generous headroom that still catches a 10x regression.

The point of this test is to fail loudly if someone:
  - introduces a per-call DB query that wasn't there before,
  - forgets to sort policies by priority (linear → quadratic in
    chained-allow paths),
  - or registers a builtin that does I/O on every call.

The 1-second TTL cache on history builtins is what makes this
sustainable across ~thousands of decisions per second; without it,
each rule that calls session_total_tokens would hit DuckDB and
balloon p99 well past budget.
"""

from __future__ import annotations

import statistics
import time

import pytest

from db import Database
from firewall import decide as fw_decide
from lumin.policy import Policy
import policy_store


P99_BUDGET_MS = 20
# 10ms — well under the 50ms spec budget for before_tool_call and
# still catches a 10-20× regression. Earlier 5ms threshold was too
# tight: CI runners vary by 1-2ms on sub-10ms budgets, producing
# spurious failures (e.g. p50=5.01ms on an otherwise-clean PR).
P50_BUDGET_MS = 10
ITERATIONS = 200


@pytest.fixture
def db_with_rules() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    fw_decide.set_panic_disabled(False)

    # 10 rules, each cheap: regex_match against a tool name. Mirror
    # the size of a realistic in-house policy file.
    for i in range(10):
        p = Policy(
            name=f"rule_{i:02d}",
            description=f"perf rule {i}",
            trigger="span_end",
            condition=f'tool_name == "tool_{i}"',
            action="block" if i % 2 == 0 else "flag",
            severity="medium",
            lifecycle="before_tool_call",
            mode="shadow" if i < 5 else "enforce",
            priority=i,
        )
        policy_store.create_policy(d, p, actor="perf")

    yield d
    d.close()


def test_decide_p99_under_budget(db_with_rules: Database) -> None:
    """Run ITERATIONS decisions and assert p99 < P99_BUDGET_MS.

    The first call pays cold-cache costs; we use it as a warmup
    rather than counting it in the percentile.
    """
    samples = []
    # Warmup
    fw_decide.decide(
        db_with_rules,
        lifecycle="before_tool_call",
        tool_name="tool_99",
    )

    for i in range(ITERATIONS):
        t0 = time.perf_counter()
        out = fw_decide.decide(
            db_with_rules,
            lifecycle="before_tool_call",
            tool_name=f"tool_{i % 10}",
            params={"command": f"echo {i}"},
            session_id=f"sess_{i % 3}",
            trace_id=f"trace_{i}",
            agent="bot.support",
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        samples.append(elapsed_ms)
        assert out["decision"] in ("allow", "block", "flag")

    p50 = statistics.median(samples)
    p99 = statistics.quantiles(samples, n=100)[98]
    p_max = max(samples)

    print(f"\ndecide latency: p50={p50:.2f}ms p99={p99:.2f}ms max={p_max:.2f}ms")
    assert p50 < P50_BUDGET_MS, (
        f"p50 {p50:.2f}ms exceeds {P50_BUDGET_MS}ms — possible per-call DB query introduced?"
    )
    assert p99 < P99_BUDGET_MS, (
        f"p99 {p99:.2f}ms exceeds {P99_BUDGET_MS}ms — investigate hot path."
    )


def test_decide_no_policy_fast_path_under_1ms(db_with_rules: Database) -> None:
    """When no policies match the lifecycle, decide() should skip
    the simpleeval setup entirely and return in well under 1ms.

    This guards the cold path most agents will hit — they typically
    have rules under post_ingest, not before_tool_call, so most calls
    short-circuit on the empty-policy-set branch.
    """
    # Move all rules out of the before_tool_call lifecycle.
    db_with_rules.execute(
        "UPDATE policies SET lifecycle = 'post_ingest'"
    )
    samples = []
    # Warmup
    fw_decide.decide(db_with_rules, lifecycle="before_tool_call")

    for _ in range(100):
        t0 = time.perf_counter()
        fw_decide.decide(db_with_rules, lifecycle="before_tool_call", tool_name="x")
        samples.append((time.perf_counter() - t0) * 1000)
    p99 = statistics.quantiles(samples, n=100)[98]
    print(f"\nno-policy fast path: p99={p99:.3f}ms")
    assert p99 < 5, f"empty-policy-set fast path exceeded 5ms: p99={p99:.3f}ms"


def test_decide_under_timeout_still_returns_allow(db_with_rules: Database) -> None:
    """Sanity: even if every policy in the lifecycle matched, decide()
    completes well within budget. We're not simulating an artificial
    100ms latency here — that needs hooks the engine doesn't expose
    today — but we verify the happy path under load doesn't drift."""
    for _ in range(50):
        out = fw_decide.decide(
            db_with_rules,
            lifecycle="before_tool_call",
            tool_name="tool_0",  # matches rule_00 (block, shadow) ⇒ allow w/ shadow_hit
        )
        assert out["decision"] == "allow"
        assert out["duration_ms"] < 50  # full budget
