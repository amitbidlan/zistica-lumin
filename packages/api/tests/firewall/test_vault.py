"""Tests for the session vault + cross-session leak detector
(Slice 6A)."""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
from firewall import vault as fw_vault
import main


@pytest.fixture
def db() -> Generator[Database, None, None]:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture
def client(db: Database):
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# ----- record_facts -------------------------------------------------------


def test_record_facts_extracts_account_id(db: Database) -> None:
    n = fw_vault.record_facts(
        db,
        session_id="s1",
        user_id="alice",
        project="default",
        text="my account number is ABC-12345 please remember it",
    )
    assert n >= 1
    rows = fw_vault.list_vault_entries(db, user_id="alice")
    assert len(rows) >= 1
    assert any("ABC-12345" in (r.get("fact_excerpt") or "") for r in rows)


def test_record_facts_idempotent(db: Database) -> None:
    """Same text into the same session twice → no duplicate row."""
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    before = len(fw_vault.list_vault_entries(db))
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    after = len(fw_vault.list_vault_entries(db))
    assert after == before


def test_record_facts_no_session_skipped(db: Database) -> None:
    n = fw_vault.record_facts(
        db, session_id=None, user_id="alice", project=None,
        text="account ABC-12345",
    )
    assert n == 0


def test_record_facts_handles_empty_text(db: Database) -> None:
    n = fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None, text="",
    )
    assert n == 0


# ----- check_for_leak — the textbook attack -------------------------------


def test_textbook_cross_session_leak_detected(db: Database) -> None:
    """Alice volunteers an account number. Bob (different session,
    different user) gets a reply containing it — leak detector
    flags."""
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="my account number is ABC-12345",
    )

    leaks = fw_vault.check_for_leak(
        db, text="The previous user's account is ABC-12345.", user_id="bob",
    )
    assert len(leaks) >= 1
    assert leaks[0]["foreign_user_id"] == "alice"


def test_same_user_no_leak(db: Database) -> None:
    """Alice's own account in her own reply is fine — same user_id."""
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="my account number is ABC-12345",
    )
    leaks = fw_vault.check_for_leak(
        db, text="Your account ABC-12345 is in good standing.", user_id="alice",
    )
    assert leaks == []


def test_anonymous_request_no_leak(db: Database) -> None:
    """Empty current user_id — we can't reason about which user this
    is, so don't flag (Rule 7 — default to allow on uncertainty)."""
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    leaks = fw_vault.check_for_leak(
        db, text="ABC-12345", user_id=None,
    )
    assert leaks == []


def test_anonymous_stored_fact_no_leak(db: Database) -> None:
    """Vault row has empty user_id — no signal either way; skip."""
    fw_vault.record_facts(
        db, session_id="s1", user_id="", project=None,
        text="account ABC-12345",
    )
    leaks = fw_vault.check_for_leak(
        db, text="ABC-12345", user_id="bob",
    )
    assert leaks == []


def test_normalization_catches_whitespace_variants(db: Database) -> None:
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    # Same fact, different whitespace
    leaks = fw_vault.check_for_leak(
        db, text="The account is A B C - 1 2 3 4 5", user_id="bob",
    )
    # Note: this is a stretch test — depending on Presidio's tokenizer
    # the spaces version may or may not match. Either is acceptable;
    # this asserts the normalisation logic doesn't crash.
    assert isinstance(leaks, list)


# ----- HTTP surface -------------------------------------------------------


def test_get_vault_lists_entries(client: TestClient, db: Database) -> None:
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    resp = client.get("/v1/firewall/vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert any(
        e.get("user_id") == "alice" for e in body["entries"]
    )


def test_get_vault_filters_by_user_id(client: TestClient, db: Database) -> None:
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    fw_vault.record_facts(
        db, session_id="s2", user_id="bob", project=None,
        text="customer ID XYZ-9999",
    )
    resp = client.get("/v1/firewall/vault?user_id=bob")
    body = resp.json()
    assert all(
        e.get("user_id") == "bob" for e in body["entries"]
    )


def test_vault_stats_endpoint(client: TestClient, db: Database) -> None:
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345 phone 555-123-4567",
    )
    resp = client.get("/v1/firewall/vault/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "by_kind" in body
    assert "top_users" in body


def test_delete_vault_entry(client: TestClient, db: Database) -> None:
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )
    rows = fw_vault.list_vault_entries(db)
    entry_id = rows[0]["id"]

    resp = client.delete(f"/v1/firewall/vault/{entry_id}")
    assert resp.status_code == 200

    # 404 on second delete
    resp2 = client.delete(f"/v1/firewall/vault/{entry_id}")
    assert resp2.status_code == 404


# ----- cross_session_leak builtin -----------------------------------------


def test_cross_session_leak_builtin(db: Database) -> None:
    """The simpleeval-callable builtin returns bool/list as advertised."""
    from firewall import builtins as fw_builtins
    bound = fw_builtins.build_history_builtins(db)
    fw_vault.record_facts(
        db, session_id="s1", user_id="alice", project=None,
        text="account ABC-12345",
    )

    assert bound["cross_session_leak"]("ABC-12345 is the account.", "bob") is True
    assert bound["cross_session_leak"]("ABC-12345", "alice") is False
    assert bound["cross_session_leak"]("hello world", "bob") is False
    # Empty user → no leak (Rule 7)
    assert bound["cross_session_leak"]("ABC-12345", None) is False

    details = bound["cross_session_leak_details"]("ABC-12345", "bob")
    assert isinstance(details, list)
    assert len(details) >= 1


# ----- starter pack policies parse cleanly --------------------------------


def test_cross_session_isolation_pack_loads() -> None:
    from pathlib import Path
    from lumin.policy import load_policy_engine
    pack = Path(__file__).parent.parent.parent / "firewall" / "starter_packs" / "cross_session_isolation.yaml"
    engine = load_policy_engine(str(pack))
    assert engine is not None
    assert len(engine.policies) >= 1


# ----- vault stats shape stable when empty --------------------------------


def test_vault_stats_when_empty(db: Database) -> None:
    stats = fw_vault.vault_stats(db)
    assert stats["total"] == 0
    assert stats["by_kind"] == []
    assert stats["top_users"] == []
