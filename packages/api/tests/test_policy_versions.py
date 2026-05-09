"""Tests for policy version history + rollback (Slice 6C, §10.5)."""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
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


def _make_policy(name: str, action: str = "block") -> Policy:
    return Policy(
        name=name,
        description="test rule",
        trigger="span_end",
        condition="True",
        action=action,
        severity="medium",
        lifecycle="post_ingest",
        mode="enforce",
        priority=0,
    )


# ----- snapshot writes on create + update --------------------------------


def test_create_records_version_1(db: Database) -> None:
    p = _make_policy("rule_a")
    policy_store.create_policy(db, p, actor="alice")
    versions = policy_store.list_versions(db, "rule_a")
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["created_by"] == "alice"


def test_update_appends_a_new_version(db: Database) -> None:
    p = _make_policy("rule_b")
    policy_store.create_policy(db, p, actor="alice")
    policy_store.update_policy(
        db, "rule_b", action="flag", actor="bob",
    )
    versions = policy_store.list_versions(db, "rule_b")
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["created_by"] == "bob"


def test_get_version(db: Database) -> None:
    p = _make_policy("rule_c")
    policy_store.create_policy(db, p, actor="alice")
    snap = policy_store.get_version(db, "rule_c", 1)
    assert snap is not None
    assert snap["version_number"] == 1
    assert "rule_c" in (snap["yaml"] or "")


def test_get_unknown_version_returns_none(db: Database) -> None:
    p = _make_policy("rule_d")
    policy_store.create_policy(db, p)
    assert policy_store.get_version(db, "rule_d", 999) is None


# ----- rollback ------------------------------------------------------------


def test_rollback_restores_earlier_action(db: Database) -> None:
    """v1: action=block. Update to action=flag (v2). Update to
    action=rewrite (v3). Roll back to v1 → current action is block."""
    policy_store.create_policy(db, _make_policy("rule_rb", action="block"))
    policy_store.update_policy(db, "rule_rb", action="flag")
    policy_store.update_policy(db, "rule_rb", action="rewrite")

    current = policy_store.get_policy(db, "rule_rb")
    assert current is not None
    assert current.action == "rewrite"

    policy_store.rollback_to_version(db, "rule_rb", 1, actor="alice")
    current = policy_store.get_policy(db, "rule_rb")
    assert current.action == "block"


def test_rollback_records_a_new_version(db: Database) -> None:
    """A rollback to v1 itself produces v(N+1) — the audit trail
    keeps showing forward motion, never rewrites history."""
    policy_store.create_policy(db, _make_policy("rule_rh"))
    policy_store.update_policy(db, "rule_rh", action="flag")
    # versions: [2, 1]
    policy_store.rollback_to_version(db, "rule_rh", 1)
    versions = policy_store.list_versions(db, "rule_rh")
    assert [v["version_number"] for v in versions] == [3, 2, 1]


def test_rollback_unknown_version_raises(db: Database) -> None:
    policy_store.create_policy(db, _make_policy("rule_un"))
    with pytest.raises(KeyError):
        policy_store.rollback_to_version(db, "rule_un", 999)


# ----- HTTP surface --------------------------------------------------------


def test_get_versions_endpoint(client: TestClient, db: Database) -> None:
    policy_store.create_policy(db, _make_policy("rule_h1"))
    policy_store.update_policy(db, "rule_h1", action="flag")
    resp = client.get("/v1/policies/rule_h1/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["policy_name"] == "rule_h1"
    assert len(body["versions"]) == 2


def test_get_versions_404_for_unknown_policy(client: TestClient) -> None:
    resp = client.get("/v1/policies/no_such/versions")
    assert resp.status_code == 404


def test_get_one_version_endpoint(client: TestClient, db: Database) -> None:
    policy_store.create_policy(db, _make_policy("rule_h2"))
    resp = client.get("/v1/policies/rule_h2/versions/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version_number"] == 1
    assert "rule_h2" in body["yaml"]


def test_get_one_version_404(client: TestClient, db: Database) -> None:
    policy_store.create_policy(db, _make_policy("rule_h3"))
    resp = client.get("/v1/policies/rule_h3/versions/999")
    assert resp.status_code == 404


def test_rollback_endpoint(client: TestClient, db: Database) -> None:
    policy_store.create_policy(db, _make_policy("rule_h4", action="block"))
    policy_store.update_policy(db, "rule_h4", action="flag")
    resp = client.post(
        "/v1/policies/rule_h4/rollback",
        json={"version_number": 1},
        headers={"X-Lumin-Actor": "alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rolled_back"] == "rule_h4"
    assert body["to_version"] == 1
    assert body["current"]["action"] == "block"


def test_rollback_endpoint_validates_payload(
    client: TestClient, db: Database,
) -> None:
    policy_store.create_policy(db, _make_policy("rule_h5"))
    resp = client.post(
        "/v1/policies/rule_h5/rollback",
        json={"version_number": "not-an-int"},
    )
    assert resp.status_code == 400


def test_rollback_endpoint_404_unknown_version(
    client: TestClient, db: Database,
) -> None:
    policy_store.create_policy(db, _make_policy("rule_h6"))
    resp = client.post(
        "/v1/policies/rule_h6/rollback",
        json={"version_number": 999},
    )
    assert resp.status_code == 404
