"""Tests for the bypass log — Slice 3 PR P / spec §14.4.

Verifies the label-driven bypass query: a span/trace labeled ``bad``
where no block-class decision fired counts as a bypass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import Database
from firewall import bypass_log as bp


@pytest.fixture
def db() -> Database:
    instance = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield instance
    instance.close()


def _seed_trace(db: Database, trace_id: str, agent: str = "bot") -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO traces (id, name, project, started_at, ingest_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [trace_id, agent, "test_project", now, now],
    )


def _seed_span(
    db: Database,
    trace_id: str,
    span_id: str,
    *,
    span_type: str = "tool",
    tool_name: str = "shell",
    output: str = "result",
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO spans (id, trace_id, type, name, tool_name, output, "
        "started_at, ended_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok')",
        [span_id, trace_id, span_type, "x", tool_name, output, now, now],
    )


def _seed_label(
    db: Database,
    label_id: str,
    trace_id: str,
    span_id: str = None,
    label: str = "bad",
    category: str = "pii_leak",
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO labels (id, trace_id, span_id, field, label, "
        "category, labeled_by, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [label_id, trace_id, span_id, "output", label, category, "ops", now],
    )


def _seed_decision(
    db: Database,
    decision_id: str,
    trace_id: str,
    decision: str = "block",
    policy: str = "test_policy",
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO decisions (id, policy_id, policy_name, lifecycle, "
        "decision, mode_at_decision, trace_id, duration_ms, decision_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [decision_id, policy, policy, "before_tool_call", decision,
         "enforce", trace_id, 1, now],
    )


# --- recent_bypasses ------------------------------------------------------


def test_label_with_no_block_is_a_bypass(db: Database) -> None:
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    _seed_label(db, "lbl-1", "tr-1", "sp-1")

    out = bp.recent_bypasses(db)
    assert len(out) == 1
    assert out[0]["trace_id"] == "tr-1"
    assert out[0]["category"] == "pii_leak"


def test_label_with_block_decision_is_not_a_bypass(db: Database) -> None:
    """When the firewall blocked something on the same trace, the
    label confirms a real positive — NOT a bypass."""
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    _seed_label(db, "lbl-1", "tr-1", "sp-1")
    _seed_decision(db, "d-1", "tr-1", decision="block")

    assert bp.recent_bypasses(db) == []


def test_label_with_rewrite_is_not_a_bypass(db: Database) -> None:
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    _seed_label(db, "lbl-1", "tr-1", "sp-1")
    _seed_decision(db, "d-1", "tr-1", decision="rewrite")
    assert bp.recent_bypasses(db) == []


def test_label_with_require_approval_is_not_a_bypass(db: Database) -> None:
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    _seed_label(db, "lbl-1", "tr-1", "sp-1")
    _seed_decision(db, "d-1", "tr-1", decision="require_approval")
    assert bp.recent_bypasses(db) == []


def test_label_with_only_flag_decision_is_a_bypass(db: Database) -> None:
    """``flag`` doesn't count as catching — operator-flagged but not
    blocked. The label says "bad" → still a bypass."""
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    _seed_label(db, "lbl-1", "tr-1", "sp-1")
    _seed_decision(db, "d-1", "tr-1", decision="flag")

    out = bp.recent_bypasses(db)
    assert len(out) == 1


def test_good_labels_are_excluded(db: Database) -> None:
    """Only ``bad`` labels are bypass candidates — ``good`` /
    ``neutral`` are positive feedback, not gaps."""
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    _seed_label(db, "lbl-1", "tr-1", "sp-1", label="good")

    assert bp.recent_bypasses(db) == []


def test_bypass_window_excludes_old_labels(db: Database) -> None:
    """``since`` filter — old labels don't surface."""
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    # Manually insert a label with a 60-day-old timestamp.
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    db.execute(
        "INSERT INTO labels (id, trace_id, span_id, field, label, category, "
        "labeled_by, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["lbl-old", "tr-1", "sp-1", "output", "bad", "pii", "ops", old],
    )

    # Default window is 30d — should be empty.
    assert bp.recent_bypasses(db) == []
    # Wide window — should surface.
    out = bp.recent_bypasses(
        db, since=datetime.now(timezone.utc) - timedelta(days=90)
    )
    assert len(out) == 1


def test_bypass_truncates_long_io(db: Database) -> None:
    """Span input/output is truncated to 500 chars in the response."""
    _seed_trace(db, "tr-1")
    long_text = "x" * 5000
    _seed_span(db, "tr-1", "sp-1", output=long_text)
    _seed_label(db, "lbl-1", "tr-1", "sp-1")

    out = bp.recent_bypasses(db)
    assert len(out[0]["span_output_preview"]) == 500
    assert out[0]["span_output_preview"].endswith("...")


def test_limit_caps_output(db: Database) -> None:
    for i in range(20):
        _seed_trace(db, f"tr-{i}")
        _seed_span(db, f"tr-{i}", f"sp-{i}")
        _seed_label(db, f"lbl-{i}", f"tr-{i}", f"sp-{i}")
    out = bp.recent_bypasses(db, limit=5)
    assert len(out) == 5


# --- bypass_summary -------------------------------------------------------


def test_summary_counts_by_category_and_tool(db: Database) -> None:
    """Summary aggregates bypass counts by category, tool, and agent."""
    # Two bypasses with category=pii_leak, one with tool=shell
    _seed_trace(db, "tr-1", agent="bot.A")
    _seed_span(db, "tr-1", "sp-1", tool_name="shell")
    _seed_label(db, "lbl-1", "tr-1", "sp-1", category="pii_leak")

    _seed_trace(db, "tr-2", agent="bot.A")
    _seed_span(db, "tr-2", "sp-2", tool_name="web_fetch")
    _seed_label(db, "lbl-2", "tr-2", "sp-2", category="pii_leak")

    _seed_trace(db, "tr-3", agent="bot.B")
    _seed_span(db, "tr-3", "sp-3", tool_name="shell")
    _seed_label(db, "lbl-3", "tr-3", "sp-3", category="injection")

    summary = bp.bypass_summary(db)
    assert summary["total"] == 3
    assert summary["by_category"]["pii_leak"] == 2
    assert summary["by_category"]["injection"] == 1
    assert summary["by_tool"]["shell"] == 2
    assert summary["by_tool"]["web_fetch"] == 1
    assert summary["by_agent"]["bot.A"] == 2


def test_summary_handles_missing_category(db: Database) -> None:
    """Labels without a category bucket as 'uncategorized'."""
    _seed_trace(db, "tr-1")
    _seed_span(db, "tr-1", "sp-1")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO labels (id, trace_id, span_id, field, label, "
        "category, labeled_by, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["lbl-1", "tr-1", "sp-1", "output", "bad", None, "ops", now],
    )
    summary = bp.bypass_summary(db)
    assert summary["by_category"]["uncategorized"] == 1


def test_summary_empty_db_returns_zero(db: Database) -> None:
    summary = bp.bypass_summary(db)
    assert summary["total"] == 0
    assert summary["by_category"] == {}
