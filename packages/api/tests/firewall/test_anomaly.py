"""Tests for the behavioral anomaly detector — Slice 3 PR Q / §11.4."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from db import Database
from firewall import anomaly


@pytest.fixture
def db() -> Database:
    instance = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield instance
    instance.close()
    anomaly.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    anomaly.reset_cache_for_tests()
    yield
    anomaly.reset_cache_for_tests()


def _seed_trace(db: Database, trace_id: str, agent: str = "bot.A") -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO traces (id, name, project, started_at, ingest_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [trace_id, agent, "test", now, now],
    )


def _seed_span(
    db: Database,
    trace_id: str,
    span_id: str,
    *,
    tool_name: str = "shell",
    input_payload: dict = None,
    session_id: str = None,
    started_at: datetime = None,
) -> None:
    if input_payload is None:
        input_payload = {"command": "ls"}
    if started_at is None:
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO spans (id, trace_id, type, name, tool_name, input, "
        "session_id, started_at, ended_at, status) "
        "VALUES (?, ?, 'tool', 'x', ?, ?, ?, ?, ?, 'ok')",
        [span_id, trace_id, tool_name, json.dumps(input_payload),
         session_id, started_at, started_at],
    )


def _seed_baseline(db: Database, agent: str = "bot.A", n: int = 15) -> None:
    """Seed enough historical calls to satisfy MIN_BASELINE_SAMPLES.
    Each call has the same shape — ``shell`` with ``{"command": "ls"}``,
    short args, and a unique session_id so the per-session frequency
    baseline is "1 call per session"."""
    _seed_trace(db, "tr-base", agent=agent)
    for i in range(n):
        _seed_span(
            db, "tr-base", f"sp-base-{i}",
            tool_name="shell",
            input_payload={"command": "ls"},
            session_id=f"sess-{i}",
        )


# --- baseline gating -------------------------------------------------------


def test_returns_zero_below_min_samples(db: Database) -> None:
    """Fewer than MIN_BASELINE_SAMPLES historical calls → 0.0
    (too noisy to score)."""
    _seed_trace(db, "tr-1", agent="bot.A")
    _seed_span(db, "tr-1", "sp-1", input_payload={"command": "ls"})

    score = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": "rm -rf /"}, agent="bot.A"
    )
    assert score == 0.0


def test_returns_zero_for_falsy_inputs(db: Database) -> None:
    assert anomaly.behavioral_anomaly_score(db, None, {}, "bot.A") == 0.0
    assert anomaly.behavioral_anomaly_score(db, "shell", {}, None) == 0.0


# --- arg-length z-score ---------------------------------------------------


def test_normal_arg_length_low_score(db: Database) -> None:
    """Calls within ~1 stddev of the baseline mean produce low scores."""
    _seed_baseline(db)
    score = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": "ls"}, agent="bot.A"
    )
    assert score < 1.0


def test_arg_length_spike_high_score(db: Database) -> None:
    """A 5000-char arg payload after a baseline of 30-char calls
    should produce a very high z-score."""
    _seed_baseline(db)
    huge_command = "x" * 5000
    score = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": huge_command}, agent="bot.A"
    )
    assert score > 5.0


# --- novel keys floor -----------------------------------------------------


def test_novel_keys_each_add_one(db: Database) -> None:
    """Calls introducing previously-unseen param keys add 1.0 each
    to the floor."""
    _seed_baseline(db)
    # Baseline only saw ``command``. Add three new keys.
    score = anomaly.behavioral_anomaly_score(
        db, "shell",
        {"command": "ls", "env": {}, "cwd": "/", "shell": "bash"},
        agent="bot.A",
    )
    assert score >= 3.0  # 3 novel keys


def test_known_keys_no_floor(db: Database) -> None:
    """Same keys as baseline → no floor contribution."""
    _seed_baseline(db)
    score = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": "ls"}, agent="bot.A"
    )
    # Score should be just the arg_len_z (low), no novel-keys floor.
    assert score < 1.0


# --- frequency z-score ----------------------------------------------------


def test_frequency_spike_in_session(db: Database) -> None:
    """If a session is making this tool call far more than baseline
    (which has 1 call per session), the frequency signal lights up."""
    _seed_baseline(db)
    # Seed a session with 50 shell calls — way above the 1-per-session baseline.
    _seed_trace(db, "tr-spike", agent="bot.A")
    for i in range(50):
        _seed_span(
            db, "tr-spike", f"sp-spike-{i}",
            tool_name="shell",
            input_payload={"command": "ls"},
            session_id="hot-session",
        )
    anomaly.reset_cache_for_tests()
    score = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": "ls"}, agent="bot.A",
        session_id="hot-session",
    )
    # Baseline distribution gets dragged toward the spike (it's part
    # of the same dataset), so the z-score ends up around 3-4 — still
    # well above the policy-rule threshold of 4.0 a tuned operator
    # would set, but not extreme. Assert it crossed the canonical
    # "noteworthy" 2-sigma bar.
    assert score > 2.0


# --- isolation between agents ---------------------------------------------


def test_baseline_per_agent(db: Database) -> None:
    """Two agents have independent baselines — bot.B's traffic doesn't
    affect bot.A's anomaly scores."""
    _seed_baseline(db, agent="bot.A")
    # bot.B has wildly different traffic — long args.
    _seed_trace(db, "tr-B", agent="bot.B")
    for i in range(15):
        _seed_span(
            db, "tr-B", f"sp-B-{i}",
            tool_name="shell",
            input_payload={"command": "x" * 4000},
        )

    # bot.A: short ls — normal.
    score_a = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": "ls"}, agent="bot.A"
    )
    # bot.B: same short ls — anomalous (their baseline is long).
    score_b = anomaly.behavioral_anomaly_score(
        db, "shell", {"command": "ls"}, agent="bot.B"
    )
    assert score_a < 1.0
    assert score_b > 1.0


# --- builtin wiring -------------------------------------------------------


def test_history_builtin_registered() -> None:
    from firewall.builtins import build_history_builtins
    # Use a fresh in-memory db just for the registration check
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        builtins = build_history_builtins(d)
        assert "behavioral_anomaly_score" in builtins
        # Baseline below threshold returns 0.0.
        assert builtins["behavioral_anomaly_score"]("shell", {}, "bot.A") == 0.0
    finally:
        d.close()


def test_policy_validator_allows_anomaly() -> None:
    from routers.policy import _ALLOWED_FUNCTIONS
    assert "behavioral_anomaly_score" in _ALLOWED_FUNCTIONS
