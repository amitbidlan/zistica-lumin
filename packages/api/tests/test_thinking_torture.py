"""API torture tests for thinking-block fields. Goal: try the ugliest
inputs we can think of and confirm round-trip and aggregation hold up."""

import concurrent.futures
import json

import duckdb
import pytest

from db import Database
from main import app


# ---------- 1. Migration: open a DB created BEFORE the new columns existed ----------


def test_migration_adds_columns_to_legacy_db(tmp_path):
    """A DB created with an older Synaptic (no span_subtype, no
    thinking_tokens) must be migrated cleanly when the new code opens
    it. ALTER TABLE ... IF NOT EXISTS handles this."""
    duck_path = str(tmp_path / "legacy.duckdb")

    # Hand-craft an old-style spans table — exact subset of columns
    # that v0.1.0 had before this PR.
    legacy_conn = duckdb.connect(duck_path)
    legacy_conn.execute("""
        CREATE TABLE traces (
            id VARCHAR PRIMARY KEY, name VARCHAR,
            input TEXT, output TEXT,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            total_tokens INTEGER DEFAULT 0,
            total_cost_usd DECIMAL(12,8) DEFAULT 0,
            quality_score FLOAT, user_id VARCHAR DEFAULT '',
            session_id VARCHAR, tags VARCHAR[], metadata JSON,
            ingest_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE spans (
            id VARCHAR PRIMARY KEY, trace_id VARCHAR NOT NULL,
            parent_span_id VARCHAR, type VARCHAR, name VARCHAR,
            input TEXT, output TEXT, model VARCHAR, provider VARCHAR,
            tokens_input INTEGER, tokens_output INTEGER,
            cost_usd DECIMAL(12,8),
            started_at TIMESTAMP NOT NULL, ended_at TIMESTAMP,
            status VARCHAR DEFAULT 'ok', error_message VARCHAR,
            tool_name VARCHAR, metadata JSON
        );
        CREATE TABLE evals (
            id VARCHAR DEFAULT gen_random_uuid() PRIMARY KEY,
            trace_id VARCHAR NOT NULL, span_id VARCHAR, name VARCHAR,
            score FLOAT, label VARCHAR, comment TEXT, source VARCHAR,
            model VARCHAR, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Insert one legacy span (no new columns) to make sure migration
    # preserves existing data.
    legacy_conn.execute(
        "INSERT INTO traces VALUES (?, 'legacy', null, null, '2026-01-01', '2026-01-01', 0, 0, null, '', null, null, null, '2026-01-01')",
        ("legacy-trace-1",),
    )
    legacy_conn.execute(
        "INSERT INTO spans (id, trace_id, name, type, started_at) VALUES (?, ?, 'old_call', 'llm', '2026-01-01')",
        ("legacy-span-1", "legacy-trace-1"),
    )
    legacy_conn.close()

    # Now open with the NEW Database class — should ALTER TABLE migrate
    db = Database(duckdb_path=duck_path, sqlite_path=str(tmp_path / "meta.sqlite"))
    cols = [r[1] for r in db.fetchall("PRAGMA table_info(spans)")]
    assert "span_subtype" in cols, f"span_subtype missing after migration: {cols}"
    assert "thinking_tokens" in cols, f"thinking_tokens missing after migration: {cols}"

    # Legacy data preserved
    legacy_row = db.fetchone_dict("SELECT * FROM spans WHERE id = 'legacy-span-1'")
    assert legacy_row is not None
    assert legacy_row["name"] == "old_call"
    assert legacy_row["span_subtype"] is None
    assert legacy_row["thinking_tokens"] is None

    # Insert new-style data into the migrated table
    db.execute(
        "INSERT INTO spans (id, trace_id, name, type, started_at, span_subtype, thinking_tokens) "
        "VALUES (?, ?, 'thinking', 'llm', '2026-05-01', 'thinking', 1234)",
        ("new-span-1", "legacy-trace-1"),
    )
    new_row = db.fetchone_dict("SELECT * FROM spans WHERE id = 'new-span-1'")
    assert new_row["span_subtype"] == "thinking"
    assert new_row["thinking_tokens"] == 1234
    db.close()


def test_double_migration_is_idempotent(tmp_path):
    """Running the schema init twice (e.g. fresh DB then reopen)
    must not error — ALTER TABLE … IF NOT EXISTS must be safe."""
    duck_path = str(tmp_path / "x.duckdb")
    sqlite_path = str(tmp_path / "x.sqlite")
    db1 = Database(duckdb_path=duck_path, sqlite_path=sqlite_path)
    db1.close()
    db2 = Database(duckdb_path=duck_path, sqlite_path=sqlite_path)
    db2.close()
    db3 = Database(duckdb_path=duck_path, sqlite_path=sqlite_path)
    db3.close()
    # No exceptions = pass


# ---------- 2. Concurrent ingest of thinking spans ----------


def test_fifty_concurrent_thinking_traces_all_round_trip(client):
    """50 client threads each posting a (parent + thinking + response)
    bundle simultaneously. All must end up queryable with correct
    span_subtype + thinking_tokens."""
    def post_one(i: int) -> int:
        spans = [
            {
                "id": f"p-{i}", "trace_id": f"t-{i}",
                "name": "claude_call", "type": "llm",
                "started_at": "2026-05-03T10:00:00Z",
                "ended_at":   "2026-05-03T10:00:03Z",
                "thinking_tokens": 1000 + i,
            },
            {
                "id": f"th-{i}", "trace_id": f"t-{i}", "parent_span_id": f"p-{i}",
                "name": "thinking", "type": "llm",
                "started_at": "2026-05-03T10:00:00.1Z",
                "ended_at":   "2026-05-03T10:00:02.5Z",
                "span_subtype": "thinking", "thinking_tokens": 1000 + i,
                "input": json.dumps({"thinking": f"thread {i}"}),
            },
            {
                "id": f"r-{i}", "trace_id": f"t-{i}", "parent_span_id": f"p-{i}",
                "name": "response", "type": "llm",
                "started_at": "2026-05-03T10:00:02.5Z",
                "ended_at":   "2026-05-03T10:00:03Z",
                "span_subtype": "response", "tokens_output": 100 + i,
            },
        ]
        r = client.post("/v1/spans", json={"spans": spans})
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(post_one, range(50)))
    assert all(c == 200 for c in codes), f"some POSTs failed: {codes}"

    # Verify every trace has its three spans with correct subtypes
    for i in range(50):
        spans = client.get(f"/v1/traces/t-{i}/spans").json()
        by_name = {s["name"]: s for s in spans}
        assert "claude_call" in by_name, f"trace {i} missing parent"
        assert by_name["thinking"]["span_subtype"] == "thinking"
        assert by_name["thinking"]["thinking_tokens"] == 1000 + i
        assert by_name["response"]["span_subtype"] == "response"
        assert by_name["response"]["tokens_output"] == 100 + i


# ---------- 3. Pathologically large reasoning ----------


def test_500kb_reasoning_text_accepted_and_returned(client):
    """An agent with extreme thinking budget could produce huge text.
    The wire format should accept it (no Content-Length limit on the
    JSON body) and DuckDB TEXT can hold it."""
    big = "REASONING " * 50_000  # ~500 KB
    client.post(
        "/v1/spans", json={"spans": [{
            "id": "big-1", "trace_id": "big-1",
            "name": "thinking", "type": "llm",
            "started_at": "2026-05-03T10:00:00Z",
            "ended_at":   "2026-05-03T10:00:01Z",
            "span_subtype": "thinking",
            "thinking_tokens": 125_000,
            "input": json.dumps({"thinking": big}),
        }]},
    )
    s = client.get("/v1/traces/big-1/spans").json()[0]
    assert s["span_subtype"] == "thinking"
    assert "REASONING " in s["input"]
    assert s["thinking_tokens"] == 125_000


# ---------- 4. Unicode in thinking ----------


def test_unicode_emoji_thinking_round_trips(client):
    weird = "推論 → なぜ 2+2=4? 🧠 ✓ こたえ: ４"
    r = client.post(
        "/v1/spans", json={"spans": [{
            "id": "u-1", "trace_id": "u-1",
            "name": "thinking", "type": "llm",
            "started_at": "2026-05-03T10:00:00Z",
            "ended_at":   "2026-05-03T10:00:01Z",
            "span_subtype": "thinking",
            "input": weird,
        }]},
    )
    assert r.status_code == 200
    s = client.get("/v1/traces/u-1/spans").json()[0]
    assert "推論" in s["input"]
    assert "🧠" in s["input"]


# ---------- 5. Negative / huge / weird thinking_tokens ----------


def test_negative_thinking_tokens_stored_as_is(client):
    """We don't validate; trust the SDK. Server should not crash."""
    r = client.post(
        "/v1/spans", json={"spans": [{
            "id": "neg-1", "trace_id": "neg-1",
            "name": "thinking", "type": "llm",
            "started_at": "2026-05-03T10:00:00Z",
            "ended_at":   "2026-05-03T10:00:01Z",
            "span_subtype": "thinking",
            "thinking_tokens": -42,  # bogus but we don't crash
        }]},
    )
    assert r.status_code == 200
    s = client.get("/v1/traces/neg-1/spans").json()[0]
    assert s["thinking_tokens"] == -42


def test_huge_thinking_tokens_count(client):
    r = client.post(
        "/v1/spans", json={"spans": [{
            "id": "huge-1", "trace_id": "huge-1",
            "name": "thinking", "type": "llm",
            "started_at": "2026-05-03T10:00:00Z",
            "ended_at":   "2026-05-03T10:00:01Z",
            "span_subtype": "thinking",
            "thinking_tokens": 2_000_000_000,  # 2B — fits in INT
        }]},
    )
    assert r.status_code == 200


# ---------- 6. Idempotent re-ingest with subtype changes ----------


def test_resubmitting_same_span_id_with_different_subtype_overwrites(client):
    """ON CONFLICT DO UPDATE means the latest write wins for span_subtype too."""
    base = {
        "id": "i-1", "trace_id": "i-1",
        "name": "claude_call", "type": "llm",
        "started_at": "2026-05-03T10:00:00Z",
        "ended_at":   "2026-05-03T10:00:01Z",
    }
    client.post("/v1/spans", json={"spans": [{**base, "span_subtype": "thinking", "thinking_tokens": 100}]})
    client.post("/v1/spans", json={"spans": [{**base, "span_subtype": "response", "thinking_tokens": None, "tokens_output": 50}]})

    s = client.get("/v1/traces/i-1/spans").json()[0]
    assert s["span_subtype"] == "response"
    assert s["thinking_tokens"] is None
    assert s["tokens_output"] == 50


# ---------- 7. Mixed thinking + tool spans in same trace ----------


def test_thinking_and_tool_spans_coexist_in_same_trace(client):
    """A real Claude agent does: thinking → tool_use → response. The
    thinking variant should not interfere with non-thinking children."""
    spans = [
        {"id": "p", "trace_id": "mix-1", "name": "claude_call", "type": "llm",
         "started_at": "2026-05-03T10:00:00Z", "ended_at": "2026-05-03T10:00:05Z"},
        {"id": "th", "trace_id": "mix-1", "parent_span_id": "p",
         "name": "thinking", "type": "llm", "span_subtype": "thinking",
         "thinking_tokens": 1000,
         "started_at": "2026-05-03T10:00:00.5Z", "ended_at": "2026-05-03T10:00:01Z"},
        {"id": "tool", "trace_id": "mix-1", "parent_span_id": "p",
         "name": "get_weather", "type": "tool", "tool_name": "get_weather",
         "input": "{\"city\":\"SF\"}",
         "started_at": "2026-05-03T10:00:01Z", "ended_at": "2026-05-03T10:00:02Z"},
        {"id": "resp", "trace_id": "mix-1", "parent_span_id": "p",
         "name": "response", "type": "llm", "span_subtype": "response",
         "tokens_output": 200,
         "started_at": "2026-05-03T10:00:04Z", "ended_at": "2026-05-03T10:00:05Z"},
    ]
    client.post("/v1/spans", json={"spans": spans})
    by = {s["name"]: s for s in client.get("/v1/traces/mix-1/spans").json()}
    assert by["thinking"]["span_subtype"] == "thinking"
    assert by["get_weather"]["span_subtype"] is None  # tool span has no subtype
    assert by["get_weather"]["tool_name"] == "get_weather"
    assert by["response"]["span_subtype"] == "response"


# ---------- 8. Subtype variant: future-proofing ----------


def test_unknown_span_subtype_value_does_not_crash(client):
    """If a future SDK invents a new subtype (say, 'tool_thinking'),
    the API must accept it — we don't enum-validate. The dashboard
    will fall through to default rendering."""
    r = client.post(
        "/v1/spans", json={"spans": [{
            "id": "future-1", "trace_id": "future-1",
            "name": "experimental", "type": "llm",
            "started_at": "2026-05-03T10:00:00Z",
            "ended_at":   "2026-05-03T10:00:01Z",
            "span_subtype": "tool_thinking_v2",
        }]},
    )
    assert r.status_code == 200
    s = client.get("/v1/traces/future-1/spans").json()[0]
    assert s["span_subtype"] == "tool_thinking_v2"
