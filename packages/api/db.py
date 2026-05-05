import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import duckdb


DUCKDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id VARCHAR PRIMARY KEY,
    name VARCHAR,
    input TEXT,
    output TEXT,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    total_tokens INTEGER DEFAULT 0,
    total_cost_usd DECIMAL(12,8) DEFAULT 0,
    quality_score FLOAT,
    user_id VARCHAR DEFAULT '',
    session_id VARCHAR,
    tags VARCHAR[],
    metadata JSON,
    ingest_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- The framework / integration that produced this trace. Set from
    -- the X-Lumin-Project request header at ingest. Lets the agent
    -- grid group "OpenClaw / Mastra / VoltAgent / default" sections.
    project VARCHAR
);

CREATE TABLE IF NOT EXISTS spans (
    id VARCHAR PRIMARY KEY,
    trace_id VARCHAR NOT NULL,
    parent_span_id VARCHAR,
    type VARCHAR,
    name VARCHAR,
    input TEXT,
    output TEXT,
    model VARCHAR,
    provider VARCHAR,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd DECIMAL(12,8),
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    status VARCHAR DEFAULT 'ok',
    error_message VARCHAR,
    tool_name VARCHAR,
    metadata JSON,
    span_subtype VARCHAR,
    thinking_tokens INTEGER,
    session_id VARCHAR,
    project VARCHAR
);

CREATE TABLE IF NOT EXISTS evals (
    id VARCHAR DEFAULT gen_random_uuid() PRIMARY KEY,
    trace_id VARCHAR NOT NULL,
    span_id VARCHAR,
    name VARCHAR,
    score FLOAT,
    label VARCHAR,
    comment TEXT,
    source VARCHAR,
    model VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Phase 4 — DB-backed policies. Authoritative once any row exists.
-- YAML is read once at startup as a one-shot bootstrap when the
-- table is empty AND LUMIN_POLICY_FILE is set. Soft-delete via
-- enabled = false preserves history of what fired against past
-- traces. Reuse of a deleted name is allowed via UPDATE.
CREATE TABLE IF NOT EXISTS policies (
    name VARCHAR PRIMARY KEY,
    description VARCHAR,
    trigger VARCHAR NOT NULL,
    condition VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    webhook_url VARCHAR,
    -- JSON array of agent names. Empty list / NULL = un-scoped.
    scope_agents JSON,
    enabled BOOLEAN DEFAULT true,
    -- Bumps on every UPDATE. The engine watches max(version) to
    -- know when to re-cache, cheaper than a full re-read per span.
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS policy_audit (
    -- Random uuid per audit row — no natural key.
    id VARCHAR DEFAULT gen_random_uuid() PRIMARY KEY,
    policy_name VARCHAR NOT NULL,
    -- "create" | "update" | "delete" — keeps the wire model honest.
    action VARCHAR NOT NULL,
    before JSON,
    after JSON,
    actor VARCHAR,  -- who made the change (HTTP basic-auth user, etc.)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS policy_violations (
    -- `id` is deterministic — sha256(policy_name + trace_id + (span_id or ''))
    -- truncated to 32 hex chars. PRIMARY KEY gives free deduplication
    -- via INSERT … ON CONFLICT DO NOTHING. Re-ingesting the same span
    -- (OTel retry, batch replay, SDK-and-server both running) cannot
    -- create duplicate violation rows.
    id VARCHAR PRIMARY KEY,
    policy_name VARCHAR NOT NULL,
    policy_description VARCHAR,
    span_id VARCHAR,
    trace_id VARCHAR NOT NULL,
    condition_text VARCHAR,
    action_taken VARCHAR,
    severity VARCHAR,
    actual_value VARCHAR,
    webhook_fired BOOLEAN DEFAULT false,
    webhook_url VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """Owns the DuckDB and SQLite connections. Single shared connection,
    serialized via a lock — DuckDB writes don't tolerate concurrency.
    """

    def __init__(self, duckdb_path: str = ":memory:", sqlite_path: str = ":memory:"):
        self._duckdb_path = duckdb_path
        self._sqlite_path = sqlite_path
        self._duck = duckdb.connect(duckdb_path)
        self._sqlite = sqlite3.connect(sqlite_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            for stmt in DUCKDB_SCHEMA.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._duck.execute(stmt)
            # Migrations for databases created before columns were added.
            # IF NOT EXISTS on ALTER COLUMN is supported in DuckDB >= 0.10
            # but we still wrap each ALTER in try/except as defense in
            # depth — the SDK must never fail because of schema fiddling.
            for migration in (
                "ALTER TABLE spans ADD COLUMN IF NOT EXISTS span_subtype VARCHAR",
                "ALTER TABLE spans ADD COLUMN IF NOT EXISTS thinking_tokens INTEGER",
                "ALTER TABLE spans ADD COLUMN IF NOT EXISTS session_id VARCHAR",
                # policy_violations table is CREATE IF NOT EXISTS above,
                # but the actual_value column was added later — keep
                # the migration here for databases created before it.
                "ALTER TABLE policy_violations ADD COLUMN IF NOT EXISTS actual_value VARCHAR",
                # Project / framework tag (X-Lumin-Project). Added so the
                # agent grid can group by integration (OpenClaw, Mastra,
                # VoltAgent, default-Python).
                "ALTER TABLE spans ADD COLUMN IF NOT EXISTS project VARCHAR",
                "ALTER TABLE traces ADD COLUMN IF NOT EXISTS project VARCHAR",
                # Backfill: any existing project value that isn't in the
                # current allowlist gets folded to "default". Catches
                # historical data where someone POSTed a free-form value
                # like "live_demo" before ingest started normalizing.
                # Idempotent — re-running rewrites already-default rows
                # to themselves.
                "UPDATE traces SET project = 'default' "
                "WHERE project IS NOT NULL AND LOWER(project) NOT IN "
                "('openclaw', 'mastra', 'voltagent', 'default')",
                "UPDATE spans SET project = 'default' "
                "WHERE project IS NOT NULL AND LOWER(project) NOT IN "
                "('openclaw', 'mastra', 'voltagent', 'default')",
                # Lowercase pass — "OpenClaw" → "openclaw" etc, so the
                # case-insensitive normalize on ingest matches the
                # case-sensitive grouping at read time.
                "UPDATE traces SET project = LOWER(project) "
                "WHERE project IS NOT NULL AND project <> LOWER(project)",
                "UPDATE spans SET project = LOWER(project) "
                "WHERE project IS NOT NULL AND project <> LOWER(project)",
            ):
                try:
                    self._duck.execute(migration)
                except Exception:
                    pass

    def execute(self, query: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._duck.execute(query, list(params))

    def fetchone(self, query: str, params: Sequence[Any] = ()) -> Optional[Tuple]:
        with self._lock:
            cur = self._duck.execute(query, list(params))
            return cur.fetchone()

    def fetchall(self, query: str, params: Sequence[Any] = ()) -> List[Tuple]:
        with self._lock:
            cur = self._duck.execute(query, list(params))
            return cur.fetchall()

    def fetchall_dict(self, query: str, params: Sequence[Any] = ()) -> List[dict]:
        with self._lock:
            cur = self._duck.execute(query, list(params))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def fetchone_dict(self, query: str, params: Sequence[Any] = ()) -> Optional[dict]:
        with self._lock:
            cur = self._duck.execute(query, list(params))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None

    def cleanup_old_traces(self, retention_days: int) -> int:
        """Delete traces (and their dependent spans + evals) older than
        ``retention_days``. Compares against ``traces.started_at``.

        Returns the number of traces deleted. Cascades manually because
        DuckDB doesn't support FOREIGN KEY ... ON DELETE CASCADE.

        Cutoff is computed in Python as naive UTC — same shape stored in
        the timestamp columns — to avoid the local-vs-UTC drift that bit
        us when relying on DuckDB's CURRENT_TIMESTAMP.
        """
        if retention_days < 0:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).replace(tzinfo=None)

        with self._lock:
            old = self._duck.execute(
                "SELECT id FROM traces WHERE started_at < ?", [cutoff]
            ).fetchall()
            if old:
                # Cascade-delete trace-bound rows BEFORE deleting traces.
                self._duck.execute(
                    "DELETE FROM spans WHERE trace_id IN "
                    "(SELECT id FROM traces WHERE started_at < ?)",
                    [cutoff],
                )
                self._duck.execute(
                    "DELETE FROM evals WHERE trace_id IN "
                    "(SELECT id FROM traces WHERE started_at < ?)",
                    [cutoff],
                )
                # Cascade to policy_violations. Without this the table
                # grew unbounded after the v1 retention task started
                # running, since it only deleted from traces/spans/evals.
                # Belongs to Rule 7's spirit — observability tables must
                # not outlive the data they reference.
                self._duck.execute(
                    "DELETE FROM policy_violations WHERE trace_id IN "
                    "(SELECT id FROM traces WHERE started_at < ?)",
                    [cutoff],
                )
                self._duck.execute(
                    "DELETE FROM traces WHERE started_at < ?", [cutoff]
                )

            # Always sweep ORPHAN violations — rows whose trace_id never
            # had a matching trace inserted. Only happens when callers
            # POST to /v1/violations directly without first ingesting
            # the trace via /v1/spans (rare, but the brutal-test SSRF
            # gauntlet surfaced this gap). Bound the sweep by `cutoff`
            # so a legitimate race (violation lands a few ms before
            # the trace stub) doesn't get GC'd.
            #
            # Run unconditionally — even when there are no old traces
            # to cascade-delete, orphans can accumulate from direct
            # /v1/violations POSTs.
            self._duck.execute(
                """
                DELETE FROM policy_violations
                WHERE created_at < ?
                  AND trace_id NOT IN (SELECT id FROM traces)
                """,
                [cutoff],
            )
            return len(old)

    def close(self) -> None:
        # CHECKPOINT before close — DuckDB normally flushes the WAL on
        # close(), but doing it explicitly first makes the failure mode
        # easier to spot (a CHECKPOINT error logs to stdout; a silent
        # close-time flush failure leaves the next startup with an
        # un-replayable WAL). Idempotent on an empty WAL — costs ~0.
        with self._lock:
            try:
                self._duck.execute("CHECKPOINT")
            except Exception:
                pass
            try:
                self._duck.close()
            except Exception:
                pass
            try:
                self._sqlite.close()
            except Exception:
                pass


def default_db() -> Database:
    """Create the production database from LUMIN_DATA_DIR (or ./data)."""
    data_dir = Path(os.environ.get("LUMIN_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Database(
        duckdb_path=str(data_dir / "traces.duckdb"),
        sqlite_path=str(data_dir / "meta.sqlite"),
    )


_db: Optional[Database] = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """FastAPI dependency. Lazily creates the production DB on first request."""
    global _db
    with _db_lock:
        if _db is None:
            _db = default_db()
        return _db
