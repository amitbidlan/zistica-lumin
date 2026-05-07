"""Tests for trace replay — Slice 3 PR M / spec §5.10 + §14.1.

Verifies:
  - Replay produces the same decision verb that decide() would
  - persist=False does NOT write to the decisions table
  - Per-span lifecycle expansion (tool span → before+after)
  - Missing trace → 404 / KeyError
  - policy_ids filter restricts the response
  - Summary counts match the decisions list
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from db import Database
from firewall import decide as fw_decide
from firewall import replay as fw_replay
from lumin.policy import Policy
import policy_store


@pytest.fixture
def db() -> Database:
    instance = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    fw_decide.set_panic_disabled(False)
    yield instance
    instance.close()


def _seed_trace_with_shell_span(
    db: Database, command: str, trace_id: str = "tr-1", span_id: str = "sp-1"
) -> None:
    """Insert a trace + a single tool span that records ``command``."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        INSERT INTO traces (id, name, project, started_at, ingest_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [trace_id, "test_agent", "test_project", now, now],
    )
    db.execute(
        """
        INSERT INTO spans (
            id, trace_id, type, name, tool_name,
            input, output, started_at, ended_at, status
        )
        VALUES (?, ?, 'tool', 'shell_call', 'shell',
                ?, ?, ?, ?, 'ok')
        """,
        [
            span_id, trace_id,
            json.dumps({"command": command}),
            "ran",
            now, now,
        ],
    )


def _install_block_rm_rule(db: Database) -> None:
    p = Policy(
        name="test_block_rm_rf",
        description="block rm -rf",
        trigger="span_end",
        condition='regex_match(str(Input.params.get("command", "")), "(?i)rm\\s+-rf\\s")',
        action="block",
        severity="critical",
        lifecycle="before_tool_call",
        mode="enforce",
        priority=100,
    )
    policy_store.create_policy(db, p, actor="test")


# --- core replay behavior --------------------------------------------------


def test_replay_returns_block_for_dangerous_command(db: Database) -> None:
    """A trace whose tool span ran ``rm -rf /`` should replay as a
    block under the rule we just installed."""
    _seed_trace_with_shell_span(db, "rm -rf /")
    _install_block_rm_rule(db)

    out = fw_replay.replay_trace(db, "tr-1")
    assert out["trace_id"] == "tr-1"
    assert out["span_count"] == 1
    assert len(out["decisions"]) == 1
    d = out["decisions"][0]
    assert d["decision"] == "block"
    assert d["policy_name"] == "test_block_rm_rf"
    assert d["lifecycle"] == "before_tool_call"


def test_replay_returns_no_decisions_for_safe_command(db: Database) -> None:
    """Safe command + the same rule = empty decisions list. We don't
    surface the 'allow with no policy matched' rows — they're noise."""
    _seed_trace_with_shell_span(db, "ls -la")
    _install_block_rm_rule(db)

    out = fw_replay.replay_trace(db, "tr-1")
    assert out["span_count"] == 1
    assert out["decisions"] == []
    assert out["summary"] == {"block": 0, "rewrite": 0, "require_approval": 0, "flag": 0, "allow": 0}


def test_replay_does_not_persist_decisions(db: Database) -> None:
    """Replay must NOT write to the ``decisions`` table — that table
    is the historical record of what *actually* happened. Replay is
    advisory."""
    _seed_trace_with_shell_span(db, "rm -rf /")
    _install_block_rm_rule(db)

    before = db.fetchone("SELECT COUNT(*) FROM decisions")
    fw_replay.replay_trace(db, "tr-1")
    after = db.fetchone("SELECT COUNT(*) FROM decisions")

    assert before[0] == after[0], "replay should not write to decisions table"


def test_replay_respects_policy_ids_filter(db: Database) -> None:
    """When policy_ids is set, only decisions matching those policies
    appear in the response."""
    _seed_trace_with_shell_span(db, "rm -rf /")
    _install_block_rm_rule(db)

    # Filter that includes our rule → block surfaces.
    out = fw_replay.replay_trace(db, "tr-1", policy_ids=["test_block_rm_rf"])
    assert len(out["decisions"]) == 1

    # Filter that excludes our rule → empty.
    out = fw_replay.replay_trace(db, "tr-1", policy_ids=["nonexistent"])
    assert out["decisions"] == []


def test_replay_unknown_trace_raises_keyerror(db: Database) -> None:
    with pytest.raises(KeyError, match="ghost"):
        fw_replay.replay_trace(db, "ghost")


def test_replay_empty_trace_id_raises(db: Database) -> None:
    with pytest.raises(ValueError):
        fw_replay.replay_trace(db, "")


def test_replay_summary_matches_decisions(db: Database) -> None:
    """Summary counts must equal the per-verb counts in decisions."""
    _seed_trace_with_shell_span(db, "rm -rf /")
    _install_block_rm_rule(db)

    out = fw_replay.replay_trace(db, "tr-1")
    counts = {"block": 0, "rewrite": 0, "require_approval": 0, "flag": 0, "allow": 0}
    for d in out["decisions"]:
        if d["decision"] in counts:
            counts[d["decision"]] += 1
    assert counts == out["summary"]


def test_replay_does_not_create_approvals(db: Database) -> None:
    """A require_approval rule replayed must NOT create a real
    approval row — that would surface a phantom in the dashboard."""
    _seed_trace_with_shell_span(db, "rm -rf /")
    p = Policy(
        name="test_approval_rule",
        description="require approval",
        trigger="span_end",
        condition='regex_match(str(Input.params.get("command", "")), "rm")',
        action="require_approval",
        severity="high",
        lifecycle="before_tool_call",
        mode="enforce",
        priority=100,
    )
    policy_store.create_policy(db, p, actor="test")

    before = db.fetchone("SELECT COUNT(*) FROM approvals")
    out = fw_replay.replay_trace(db, "tr-1")
    after = db.fetchone("SELECT COUNT(*) FROM approvals")

    assert before[0] == after[0]
    # But the decision IS surfaced in the replay output.
    assert any(d["decision"] == "require_approval" for d in out["decisions"])


# --- decide(persist=False) directly ----------------------------------------


def test_decide_persist_false_skips_writes(db: Database) -> None:
    """The persist=False kwarg on decide() itself short-circuits the
    decisions table write — verified independently of replay."""
    _install_block_rm_rule(db)

    before = db.fetchone("SELECT COUNT(*) FROM decisions")
    resp = fw_decide.decide(
        db,
        lifecycle="before_tool_call",
        tool_name="shell",
        params={"command": "rm -rf /"},
        persist=False,
    )
    after = db.fetchone("SELECT COUNT(*) FROM decisions")

    assert resp["decision"] == "block"
    assert before[0] == after[0]


def test_decide_persist_true_writes(db: Database) -> None:
    """Sanity: persist=True (default) DOES write."""
    _install_block_rm_rule(db)

    before = db.fetchone("SELECT COUNT(*) FROM decisions")
    fw_decide.decide(
        db,
        lifecycle="before_tool_call",
        tool_name="shell",
        params={"command": "rm -rf /"},
    )
    after = db.fetchone("SELECT COUNT(*) FROM decisions")

    assert after[0] == before[0] + 1
