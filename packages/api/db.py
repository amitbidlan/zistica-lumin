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
    ingest_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    session_id VARCHAR
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
            if not old:
                return 0
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
            self._duck.execute(
                "DELETE FROM traces WHERE started_at < ?", [cutoff]
            )
            return len(old)

    def close(self) -> None:
        try:
            self._duck.close()
        except Exception:
            pass
        try:
            self._sqlite.close()
        except Exception:
            pass


def default_db() -> Database:
    """Create the production database from SYNAPTIC_DATA_DIR (or ./data)."""
    data_dir = Path(os.environ.get("SYNAPTIC_DATA_DIR", "./data"))
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
