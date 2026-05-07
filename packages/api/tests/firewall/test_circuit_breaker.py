"""Tests for the auto circuit breaker — Tier 4.1 of SLICE_2_PLAN.md.

The breaker watches each policy's fire rate and, when a single rule
exceeds ``LUMIN_AUTO_TRIP_FIRES_PER_MINUTE`` fires in 60s, flips the
policy's ``circuit_breaker_state`` to 'tripped'. ``_applicable_policies``
already excludes tripped policies, so subsequent decide() calls skip
the offending rule.
"""

from __future__ import annotations

from typing import List

import pytest

from db import Database
from firewall import decide as fw_decide
from lumin.policy import Policy
import policy_store


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    fw_decide.set_panic_disabled(False)
    fw_decide.reset_fire_rate_tracker_for_tests()
    yield d
    fw_decide.set_panic_disabled(False)
    fw_decide.reset_fire_rate_tracker_for_tests()
    d.close()


def _mk_policy(
    db: Database,
    *,
    name: str,
    lifecycle: str = "before_tool_call",
    mode: str = "enforce",
    priority: int = 0,
    action: str = "block",
    condition: str = "True",
) -> Policy:
    p = Policy(
        name=name,
        description=f"test policy {name}",
        trigger="span_end",
        condition=condition,
        action=action,
        severity="medium",
        scope_agents=[],
        lifecycle=lifecycle,
        mode=mode,
        priority=priority,
    )
    return policy_store.create_policy(db, p, actor="test")


# ---- _check_circuit_breaker / _track_fire ---------------------------------


def test_check_returns_false_for_low_fire_rate() -> None:
    fw_decide.reset_fire_rate_tracker_for_tests()
    for _ in range(5):
        fw_decide._track_fire("policy_a")
    assert fw_decide._check_circuit_breaker("policy_a") is False


def test_check_returns_true_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fire one more time than the threshold and the breaker trips."""
    monkeypatch.setattr(fw_decide, "_AUTO_TRIP_FIRES_PER_MINUTE", 10)
    fw_decide.reset_fire_rate_tracker_for_tests()
    # 10 is the threshold — must exceed it (>) to trip per spec.
    for _ in range(11):
        fw_decide._track_fire("noisy_rule")
    assert fw_decide._check_circuit_breaker("noisy_rule") is True


def test_window_rolls_off_after_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timestamps older than 60s must drop out of the window so a
    quiet recovery period clears the count."""
    monkeypatch.setattr(fw_decide, "_AUTO_TRIP_FIRES_PER_MINUTE", 5)
    fw_decide.reset_fire_rate_tracker_for_tests()

    # Pretend it's t=1000 and fire 6 times.
    fake_t = {"now": 1000.0}
    monkeypatch.setattr(fw_decide, "_now_seconds", lambda: fake_t["now"])
    for _ in range(6):
        fw_decide._track_fire("rule_x")
    assert fw_decide._check_circuit_breaker("rule_x") is True

    # Advance 61s — every old timestamp is now stale.
    fake_t["now"] = 1061.0
    assert fw_decide._check_circuit_breaker("rule_x") is False


def test_disabled_when_threshold_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LUMIN_AUTO_TRIP_FIRES_PER_MINUTE=0 disables the breaker — a
    rule firing 1000+ times per minute still won't auto-trip."""
    monkeypatch.setattr(fw_decide, "_AUTO_TRIP_FIRES_PER_MINUTE", 0)
    fw_decide.reset_fire_rate_tracker_for_tests()
    for _ in range(1000):
        fw_decide._track_fire("anything")
    assert fw_decide._check_circuit_breaker("anything") is False


# ---- _maybe_auto_trip ----------------------------------------------------


def test_auto_trip_updates_db_state(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the threshold is exceeded, _maybe_auto_trip must write
    'tripped' to the policy row."""
    monkeypatch.setattr(fw_decide, "_AUTO_TRIP_FIRES_PER_MINUTE", 3)
    _mk_policy(db, name="will_trip")
    fw_decide.reset_fire_rate_tracker_for_tests()
    for _ in range(5):
        fw_decide._maybe_auto_trip(db, "will_trip")
    row = db.fetchone(
        "SELECT circuit_breaker_state FROM policies WHERE name = ?",
        ["will_trip"],
    )
    assert row is not None
    assert row[0] == "tripped"


# ---- end-to-end via decide() ---------------------------------------------


def test_decide_auto_trips_after_repeated_fires(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the full decide() path past the threshold; the policy's
    DB row must flip to 'tripped' and subsequent calls must skip it."""
    monkeypatch.setattr(fw_decide, "_AUTO_TRIP_FIRES_PER_MINUTE", 5)
    _mk_policy(db, name="hot_rule", action="block", condition="True")

    decisions: List[str] = []
    for _ in range(7):
        out = fw_decide.decide(
            db, lifecycle="before_tool_call", tool_name="shell"
        )
        decisions.append(out["decision"])

    # First 6 fired (block), the 7th sees the policy tripped and
    # falls through to allow.
    assert "block" in decisions
    assert decisions[-1] == "allow"

    row = db.fetchone(
        "SELECT circuit_breaker_state FROM policies WHERE name = ?",
        ["hot_rule"],
    )
    assert row is not None
    assert row[0] == "tripped"

    # And one more decide() call confirms the policy stays skipped —
    # _applicable_policies filters it out at the SQL layer.
    out = fw_decide.decide(
        db, lifecycle="before_tool_call", tool_name="shell"
    )
    assert out["decision"] == "allow"


def test_decide_respects_disabled_auto_trip(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With LUMIN_AUTO_TRIP_FIRES_PER_MINUTE=0, repeated fires never
    flip the policy state."""
    monkeypatch.setattr(fw_decide, "_AUTO_TRIP_FIRES_PER_MINUTE", 0)
    _mk_policy(db, name="immortal", action="block", condition="True")

    for _ in range(150):
        out = fw_decide.decide(
            db, lifecycle="before_tool_call", tool_name="shell"
        )
        assert out["decision"] == "block"

    row = db.fetchone(
        "SELECT circuit_breaker_state FROM policies WHERE name = ?",
        ["immortal"],
    )
    assert row is not None
    assert row[0] == "ok"
