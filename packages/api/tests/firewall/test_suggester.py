"""Tests for the pattern suggester (Slice 3 PR D, spec §5.5 / §11)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from db import Database
from firewall import decide as fw_decide
from firewall import suggester
import policy_store
from lumin.policy import Policy


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    fw_decide.set_panic_disabled(False)
    yield d
    d.close()


def _seed_decision(db: Database, **overrides) -> str:
    import uuid
    decision_id = "dec_" + uuid.uuid4().hex[:24]
    row = {
        "id": decision_id,
        "policy_id": "test_policy",
        "policy_name": "test_policy",
        "lifecycle": "before_tool_call",
        "decision": "block",
        "mode_at_decision": "enforce",
        "reason": "matched bad pattern",
        "trace_id": "trace-x",
        "span_id": "span-y",
        "session_id": "sess-1",
        "agent": "openclaw",
        "project": "test",
        "tool_name": "exec",
        "matched_field": "command",
        "matched_value_truncated": "rm -rf /tmp/sensitive",
        "decision_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "duration_ms": 3,
        "metadata": None,
    }
    row.update(overrides)
    db.execute(
        """
        INSERT INTO decisions (
            id, policy_id, policy_name, lifecycle, decision,
            mode_at_decision, reason, trace_id, span_id,
            session_id, agent, project, tool_name,
            matched_field, matched_value_truncated,
            decision_at, duration_ms, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row[k] for k in (
                "id", "policy_id", "policy_name", "lifecycle", "decision",
                "mode_at_decision", "reason", "trace_id", "span_id",
                "session_id", "agent", "project", "tool_name",
                "matched_field", "matched_value_truncated",
                "decision_at", "duration_ms", "metadata",
            )
        ],
    )
    return decision_id


# ---- module-level functions ----------------------------------------------


def test_suggest_from_decision_returns_draft(db: Database):
    did = _seed_decision(db)
    out = suggester.suggest_from_decision(db, did)
    assert out["id"].startswith("sug_")
    assert out["decision_id"] == did
    assert out["template"] == "from_decision"
    draft = out["draft"]
    assert draft["mode"] == "shadow"  # §10.1
    assert draft["action"] == "block"
    assert draft["lifecycle"] == "before_tool_call"
    # Pattern from matched_value should appear in condition (escaped)
    assert "rm" in draft["condition"]


def test_suggest_unknown_decision_raises():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        with pytest.raises(KeyError):
            suggester.suggest_from_decision(db, "dec_does_not_exist")
    finally:
        db.close()


def test_suggest_persists_to_pattern_suggestions_table(db: Database):
    did = _seed_decision(db)
    out = suggester.suggest_from_decision(db, did)
    rows = db.fetchall_dict(
        "SELECT * FROM pattern_suggestions WHERE id = ?", [out["id"]]
    )
    assert len(rows) == 1
    assert rows[0]["source_violation_id"] == did
    assert rows[0]["template"] == "from_decision"
    assert rows[0]["draft_yaml"]


def test_get_suggestion_round_trip(db: Database):
    did = _seed_decision(db)
    out1 = suggester.suggest_from_decision(db, did)
    out2 = suggester.get_suggestion(db, out1["id"])
    assert out2 is not None
    assert out2["id"] == out1["id"]
    assert out2["decision_id"] == did


def test_promote_creates_real_policy(db: Database):
    did = _seed_decision(db)
    out = suggester.suggest_from_decision(db, did)
    saved = suggester.promote_suggestion(db, out["id"], name="my_promoted_rule")
    assert saved.name == "my_promoted_rule"
    assert saved.mode == "shadow"
    assert saved.action == "block"
    # Suggestion is marked promoted
    row = db.fetchone_dict(
        "SELECT promoted_to_policy_id FROM pattern_suggestions WHERE id = ?",
        [out["id"]],
    )
    assert row["promoted_to_policy_id"] == "my_promoted_rule"


def test_promote_twice_raises(db: Database):
    did = _seed_decision(db)
    out = suggester.suggest_from_decision(db, did)
    suggester.promote_suggestion(db, out["id"], name="first")
    with pytest.raises(ValueError, match="already promoted"):
        suggester.promote_suggestion(db, out["id"], name="second")


def test_dismiss_then_promote_raises(db: Database):
    did = _seed_decision(db)
    out = suggester.suggest_from_decision(db, did)
    suggester.dismiss_suggestion(db, out["id"])
    with pytest.raises(ValueError, match="dismissed"):
        suggester.promote_suggestion(db, out["id"], name="should_fail")


# ---- HTTP endpoints ------------------------------------------------------


def test_suggest_endpoint(client, db):
    did = _seed_decision(db)
    r = client.post("/v1/policies/suggest", json={"decision_id": did})
    assert r.status_code == 200
    body = r.json()
    assert body["decision_id"] == did
    assert "draft_yaml" in body
    assert body["forecast"]["count"] >= 1  # the seeded decision counts


def test_suggest_endpoint_404(client):
    r = client.post("/v1/policies/suggest", json={"decision_id": "nope"})
    assert r.status_code == 404


def test_promote_endpoint(client, db):
    did = _seed_decision(db)
    r1 = client.post("/v1/policies/suggest", json={"decision_id": did})
    sid = r1.json()["id"]

    r2 = client.post(
        f"/v1/policies/suggest/{sid}/promote",
        json={"name": "promoted_via_api"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["name"] == "promoted_via_api"
    assert body["mode"] == "shadow"

    # Real policy in DB
    rows = db.fetchall_dict(
        "SELECT name FROM policies WHERE name = ?", ["promoted_via_api"]
    )
    assert len(rows) == 1


def test_dismiss_endpoint(client, db):
    did = _seed_decision(db)
    r1 = client.post("/v1/policies/suggest", json={"decision_id": did})
    sid = r1.json()["id"]
    r2 = client.post(f"/v1/policies/suggest/{sid}/dismiss")
    assert r2.status_code == 200
    assert r2.json()["dismissed"] is True


def test_promote_after_dismiss_409(client, db):
    did = _seed_decision(db)
    r1 = client.post("/v1/policies/suggest", json={"decision_id": did})
    sid = r1.json()["id"]
    client.post(f"/v1/policies/suggest/{sid}/dismiss")
    r3 = client.post(f"/v1/policies/suggest/{sid}/promote", json={"name": "x"})
    assert r3.status_code == 409
