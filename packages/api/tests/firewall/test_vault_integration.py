"""End-to-end integration test for the cross-session vault
(brutal-test fix — verifies the bug found while attacking the API
on 2026-05-09).

The unit tests in ``test_vault.py`` exercise the builtin and the
helper functions directly — they pass even when the wiring through
DecideRequest → namespace → policy condition is broken. This file
hits the actual ``POST /v1/policy/decide`` HTTP path with a
``user_id`` and verifies the cross_session_leak rule fires
end-to-end, which is the only test that catches a regression in
any of the four layers.
"""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
from firewall import vault as fw_vault
from lumin.policy import Policy
import main
import policy_store


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


def _install_leak_rule(db: Database) -> None:
    """Set up the canonical cross-session-leak rule in enforce mode
    so a positive test actually fires (not just records-shadow)."""
    p = Policy(
        name="cross_session_leak_block",
        description="Block when reply contains another user's vault fact.",
        trigger="span_end",
        condition="cross_session_leak(Output.text, user_id)",
        action="block",
        severity="high",
        lifecycle="after_proxy_call",
        mode="enforce",
        priority=92,
    )
    policy_store.create_policy(db, p, actor="test")


# ----- The textbook attack — actual HTTP path ----------------------------


def test_decide_blocks_when_other_user_fact_in_output(
    client: TestClient, db: Database,
) -> None:
    """Alice volunteers an account number, Bob's reply contains it,
    `decide()` returns block. Walks every layer that was broken:
    DecideRequest model accepts user_id, decide() forwards it,
    _build_namespace binds it, the rule's condition references it,
    cross_session_leak builtin matches the vault."""
    fw_vault.record_facts(
        db, session_id="sess-A", user_id="alice", project=None,
        text="my account number is BRT-99999",
    )
    _install_leak_rule(db)

    resp = client.post(
        "/v1/policy/decide",
        json={
            "lifecycle": "after_proxy_call",
            "output": {"text": "The previous user's account is BRT-99999."},
            "session_id": "sess-B",
            "user_id": "bob",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "block", body
    assert body["policy_name"] == "cross_session_leak_block"


def test_decide_allows_when_same_user_says_own_fact(
    client: TestClient, db: Database,
) -> None:
    """Alice's own account in her own reply is NOT a leak — same
    user_id binding."""
    fw_vault.record_facts(
        db, session_id="sess-A", user_id="alice", project=None,
        text="my account number is BRT-99999",
    )
    _install_leak_rule(db)

    resp = client.post(
        "/v1/policy/decide",
        json={
            "lifecycle": "after_proxy_call",
            "output": {"text": "Your account BRT-99999 is in good standing."},
            "session_id": "sess-A",
            "user_id": "alice",
        },
    )
    body = resp.json()
    assert body["decision"] == "allow", body


def test_decide_allows_when_user_id_absent(
    client: TestClient, db: Database,
) -> None:
    """Empty user_id means we can't reason about whose request this
    is — Rule 7 says default to allow on uncertainty."""
    fw_vault.record_facts(
        db, session_id="sess-A", user_id="alice", project=None,
        text="my account number is BRT-99999",
    )
    _install_leak_rule(db)

    resp = client.post(
        "/v1/policy/decide",
        json={
            "lifecycle": "after_proxy_call",
            "output": {"text": "BRT-99999"},
            "session_id": "sess-B",
            # user_id intentionally omitted
        },
    )
    body = resp.json()
    assert body["decision"] == "allow", body


def test_decide_allows_when_text_unrelated_to_vault(
    client: TestClient, db: Database,
) -> None:
    """Sanity — Bob's reply contains nothing from Alice's vault, no
    leak, no block."""
    fw_vault.record_facts(
        db, session_id="sess-A", user_id="alice", project=None,
        text="my account number is BRT-99999",
    )
    _install_leak_rule(db)

    resp = client.post(
        "/v1/policy/decide",
        json={
            "lifecycle": "after_proxy_call",
            "output": {"text": "Hi! How can I help you today?"},
            "session_id": "sess-B",
            "user_id": "bob",
        },
    )
    body = resp.json()
    assert body["decision"] == "allow", body


# ----- Validator allowlist accepts vault builtins ------------------------


def test_validator_accepts_cross_session_leak(
    client: TestClient,
) -> None:
    """A policy authored via /v1/policies referencing the vault
    builtin must validate cleanly — proves the validator's
    _ALLOWED_FUNCTIONS allowlist was updated."""
    resp = client.post(
        "/v1/policies",
        json={
            "name": "vault_check_via_api",
            "description": "Authored via API.",
            "trigger": "span_end",
            "condition": "cross_session_leak(Output.text, user_id)",
            "action": "rewrite",
            "severity": "high",
            "lifecycle": "after_proxy_call",
            "mode": "shadow",
        },
    )
    assert resp.status_code == 201, resp.text


def test_validator_accepts_user_id_bare_name(client: TestClient) -> None:
    """Bare ``user_id`` reference is in _ALLOWED_NAMES so a
    condition like ``user_id == 'admin'`` is valid."""
    resp = client.post(
        "/v1/policies",
        json={
            "name": "user_id_check",
            "description": "Bare-name test.",
            "trigger": "span_end",
            "condition": "user_id == 'admin'",
            "action": "flag",
            "severity": "low",
            "lifecycle": "before_proxy_call",
            "mode": "shadow",
        },
    )
    assert resp.status_code == 201, resp.text
