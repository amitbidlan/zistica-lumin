"""Tests for firewall schema migrations (§4 of AGENT_FIREWALL_SPEC.md).

Verifies that:
  - All 5 new tables come into existence on a fresh DB
  - All 6 new columns land on the existing ``policies`` table with
    sensible defaults
  - Existing-DB upgrade is idempotent (running migrations twice is
    a no-op, no column-already-exists explosions)
  - Existing post-ingest policies keep ``mode='enforce'`` after
    migration so the legacy violation pipeline doesn't silently
    break on upgrade
"""

from __future__ import annotations

import duckdb

from db import Database
from firewall import migrations


def _table_exists(db: Database, name: str) -> bool:
    row = db.fetchone(
        "SELECT 1 FROM duckdb_tables WHERE table_name = ?", [name]
    )
    return row is not None


def _column_names(db: Database, table: str) -> set[str]:
    rows = db.fetchall(
        "SELECT column_name FROM duckdb_columns WHERE table_name = ?",
        [table],
    )
    return {r[0] for r in rows}


def test_all_new_tables_created_on_fresh_db():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        for table in (
            "decisions",
            "approvals",
            "labels",
            "pattern_suggestions",
            "policy_versions",
        ):
            assert _table_exists(db, table), f"{table} not created"
    finally:
        db.close()


def test_policies_table_extended():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        cols = _column_names(db, "policies")
        for col in (
            "lifecycle",
            "mode",
            "priority",
            "on_timeout",
            "circuit_breaker_state",
            "on_internal_error",
        ):
            assert col in cols, f"policies.{col} missing"
    finally:
        db.close()


def test_existing_policies_default_to_enforce_mode():
    """Back-compat invariant: rows that existed BEFORE the firewall
    migration shouldn't suddenly become shadow-mode (which would
    silently disable enforcement of the existing post-ingest pipeline).
    """
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        # Insert a row that doesn't set mode explicitly — the column
        # default should kick in.
        db.execute(
            """
            INSERT INTO policies (name, trigger, condition, action, severity)
            VALUES (?, ?, ?, ?, ?)
            """,
            ["legacy", "span_end", "True", "log", "low"],
        )
        row = db.fetchone_dict("SELECT mode, lifecycle FROM policies WHERE name = ?", ["legacy"])
        assert row is not None
        assert row["mode"] == "enforce", "existing rows must default to enforce, not shadow"
        assert row["lifecycle"] == "post_ingest", "existing rows default to post_ingest"
    finally:
        db.close()


def test_migrations_are_idempotent():
    """Running migrations twice in a row should not error — the IF
    NOT EXISTS clauses guard against double-creation, and the
    individual ALTER ADD COLUMN IF NOT EXISTS clauses guard the
    extension paths.
    """
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        # Re-applying directly through the connection should be a no-op.
        migrations.apply(db._duck)
        migrations.apply(db._duck)
        # If we get here without raising, the test passes. Sanity-check
        # the tables are still queryable.
        assert _table_exists(db, "decisions")
    finally:
        db.close()


def test_decisions_table_indexes_present():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        rows = db.fetchall(
            "SELECT index_name FROM duckdb_indexes WHERE table_name = 'decisions'"
        )
        names = {r[0] for r in rows}
        for expected in (
            "idx_decisions_trace_id",
            "idx_decisions_policy_id",
            "idx_decisions_decision_at",
            "idx_decisions_decision",
        ):
            assert expected in names, f"index {expected} not created"
    finally:
        db.close()


def test_decisions_table_accepts_canonical_row():
    """Schema sanity: a row matching the spec §4.2 shape should
    insert + read back."""
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        db.execute(
            """
            INSERT INTO decisions (
                id, policy_id, policy_name, lifecycle, decision,
                mode_at_decision, reason, trace_id, span_id, session_id,
                agent, project, tool_name, matched_field,
                matched_value_truncated, decision_at, duration_ms, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "dec-001", "pol-001", "block_rm_rf",
                "before_tool_call", "block", "enforce",
                "Destructive shell.", "trace-1", "span-1", "sess-1",
                "bot.support", "openclaw", "shell",
                "params.command", "rm -rf /tmp/cache",
                "2026-05-07 12:00:00", 4, "{}",
            ],
        )
        row = db.fetchone_dict("SELECT * FROM decisions WHERE id = ?", ["dec-001"])
        assert row is not None
        assert row["decision"] == "block"
        assert row["mode_at_decision"] == "enforce"
        assert row["matched_field"] == "params.command"
    finally:
        db.close()
