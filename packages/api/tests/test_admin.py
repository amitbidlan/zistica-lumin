"""Tests for the admin / ops endpoints (Slice 5C)."""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
import main


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    duck = tmp_path / "admin-test.duckdb"
    sqlite = tmp_path / "admin-test.sqlite"
    d = Database(duckdb_path=str(duck), sqlite_path=str(sqlite))
    yield d
    d.close()


@pytest.fixture
def client(db: Database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LUMIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LUMIN_BACKUP_DIR", str(tmp_path / "backups"))
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# ----- /v1/admin/health ----------------------------------------------------


def test_admin_health_ok_when_everything_works(client: TestClient) -> None:
    resp = client.get("/v1/admin/health")
    assert resp.status_code == 200
    body = resp.json()
    component_names = {c["name"] for c in body["components"]}
    assert "database" in component_names
    assert "policy_engine" in component_names
    assert "webhook_dlq" in component_names
    assert "backup_dir" in component_names


def test_admin_health_marks_missing_backup_dir(client: TestClient, monkeypatch, tmp_path):
    monkeypatch.setenv("LUMIN_BACKUP_DIR", str(tmp_path / "does-not-exist"))
    resp = client.get("/v1/admin/health")
    body = resp.json()
    backup = next(c for c in body["components"] if c["name"] == "backup_dir")
    assert backup["status"] == "degraded"


# ----- /v1/admin/retention -------------------------------------------------


def test_get_retention_returns_defaults(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LUMIN_RETENTION_DAYS", "30")
    resp = client.get("/v1/admin/retention")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 30
    assert "backup_dir" in body


def test_retention_cleanup_endpoint(client: TestClient) -> None:
    # No traces in the DB → cleanup deletes 0
    resp = client.post(
        "/v1/admin/retention/cleanup", json={"days": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_traces"] == 0
    assert "cutoff" in body


def test_retention_cleanup_rejects_invalid_days(client: TestClient) -> None:
    resp = client.post(
        "/v1/admin/retention/cleanup", json={"days": -5},
    )
    assert resp.status_code == 422  # Pydantic validation


# ----- /v1/admin/backups ---------------------------------------------------


def test_list_backups_empty_at_start(client: TestClient) -> None:
    resp = client.get("/v1/admin/backups")
    assert resp.status_code == 200
    assert resp.json()["backups"] == []


def test_create_and_list_backup(client: TestClient) -> None:
    create = client.post("/v1/admin/backups", json={"name": "smoke"})
    assert create.status_code == 200, create.text
    body = create.json()
    assert body["name"] == "smoke"
    assert body["size_bytes"] >= 0

    listing = client.get("/v1/admin/backups").json()
    names = {b["name"] for b in listing["backups"]}
    assert "smoke" in names


def test_create_backup_rejects_traversal(client: TestClient) -> None:
    resp = client.post("/v1/admin/backups", json={"name": "../escape"})
    assert resp.status_code == 400


def test_create_backup_rejects_uppercase(client: TestClient) -> None:
    resp = client.post("/v1/admin/backups", json={"name": "SHOUTY"})
    assert resp.status_code == 400


def test_create_backup_default_name_is_timestamp(client: TestClient) -> None:
    resp = client.post("/v1/admin/backups", json={})
    body = resp.json()
    assert body["name"].startswith("snap_")


def test_create_backup_rejects_duplicate(client: TestClient) -> None:
    first = client.post("/v1/admin/backups", json={"name": "dup"})
    assert first.status_code == 200
    second = client.post("/v1/admin/backups", json={"name": "dup"})
    assert second.status_code == 409


def test_delete_backup(client: TestClient) -> None:
    client.post("/v1/admin/backups", json={"name": "tmpdel"})
    resp = client.delete("/v1/admin/backups/tmpdel")
    assert resp.status_code == 200
    assert client.delete("/v1/admin/backups/tmpdel").status_code == 404


# ----- /v1/admin/backups/{name}/restore -----------------------------------


def test_restore_requires_confirm_true(client: TestClient) -> None:
    client.post("/v1/admin/backups", json={"name": "before-restore"})
    resp = client.post(
        "/v1/admin/backups/before-restore/restore", json={"confirm": False},
    )
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"]


def test_restore_404_when_backup_missing(client: TestClient) -> None:
    resp = client.post(
        "/v1/admin/backups/no_such/restore", json={"confirm": True},
    )
    assert resp.status_code == 404


def test_restore_round_trip(client: TestClient, db: Database) -> None:
    """Create backup → mutate state → restore → verify state reverted."""
    # Seed a trace
    db.execute(
        "INSERT INTO traces (id, name, started_at) VALUES (?, ?, ?)",
        ["t1", "before", "2026-01-01 00:00:00"],
    )

    # Snapshot
    create = client.post("/v1/admin/backups", json={"name": "rt"})
    assert create.status_code == 200

    # Mutate after the snapshot
    db.execute(
        "INSERT INTO traces (id, name, started_at) VALUES (?, ?, ?)",
        ["t2", "after", "2026-01-02 00:00:00"],
    )
    rows = db.fetchall("SELECT id FROM traces ORDER BY id")
    assert {r[0] for r in rows} == {"t1", "t2"}

    # Restore
    resp = client.post(
        "/v1/admin/backups/rt/restore", json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text

    # Verify state reverted to snapshot moment
    rows = db.fetchall("SELECT id FROM traces ORDER BY id")
    assert {r[0] for r in rows} == {"t1"}


# ----- /v1/admin/firewall/profile (Slice 5: dashboard-managed config) -------


def test_firewall_profile_get_empty_default(client: TestClient) -> None:
    """Brand-new install: GET returns an empty profile, not 404."""
    resp = client.get("/v1/admin/firewall/profile?agent_id=_default")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "_default"
    assert body["security_profile"] is None
    assert body["overrides"] == {}


def test_firewall_profile_put_then_get_round_trip(client: TestClient) -> None:
    """PUT sets profile + overrides; GET returns them verbatim."""
    payload = {
        "security_profile": "strict",
        "overrides": {"blockShellTools": False, "hideOtherUsersData": True},
    }
    resp = client.put(
        "/v1/admin/firewall/profile?agent_id=_default", json=payload,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["security_profile"] == "strict"
    assert body["overrides"] == payload["overrides"]
    assert body["updated_by"] == "dashboard"

    # GET re-reads the persisted row
    resp = client.get("/v1/admin/firewall/profile?agent_id=_default")
    assert resp.status_code == 200
    body2 = resp.json()
    assert body2["security_profile"] == "strict"
    assert body2["overrides"] == payload["overrides"]


def test_firewall_profile_put_rejects_unknown_profile(client: TestClient) -> None:
    """Validation: typo in profile name → 400, not silent persistence."""
    resp = client.put(
        "/v1/admin/firewall/profile?agent_id=_default",
        json={"security_profile": "BOGUS_PROFILE"},
    )
    assert resp.status_code == 400
    assert "BOGUS_PROFILE" in resp.json()["detail"]


def test_firewall_profile_put_drops_unknown_override_keys(client: TestClient) -> None:
    """Defense against typo'd toggles silently being persisted."""
    resp = client.put(
        "/v1/admin/firewall/profile?agent_id=_default",
        json={
            "security_profile": "standard",
            "overrides": {
                "blockShellTools": True,        # known — kept
                "blokShellToolz": False,         # typo — dropped
                "shareInformationWithEnemy": True,  # bogus — dropped
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "blockShellTools" in body["overrides"]
    assert "blokShellToolz" not in body["overrides"]
    assert "shareInformationWithEnemy" not in body["overrides"]


def test_firewall_profile_put_is_upsert(client: TestClient) -> None:
    """PUT replaces the row. Second PUT shouldn't pile up duplicates."""
    for profile in ["standard", "light", "strict"]:
        resp = client.put(
            "/v1/admin/firewall/profile?agent_id=_default",
            json={"security_profile": profile, "overrides": {}},
        )
        assert resp.status_code == 200, resp.text
    # Final state
    resp = client.get("/v1/admin/firewall/profile?agent_id=_default")
    assert resp.json()["security_profile"] == "strict"


def test_firewall_profile_per_agent_falls_back_to_default(client: TestClient) -> None:
    """Agent without its own row reads from _default."""
    client.put(
        "/v1/admin/firewall/profile?agent_id=_default",
        json={"security_profile": "strict", "overrides": {}},
    )
    resp = client.get("/v1/admin/firewall/profile?agent_id=customer-support-bot")
    assert resp.status_code == 200
    body = resp.json()
    # Returned id is the _default row's id, profile inherited
    assert body["security_profile"] == "strict"


def test_firewall_profile_per_agent_overrides_default(client: TestClient) -> None:
    """Agent-specific row beats _default."""
    client.put(
        "/v1/admin/firewall/profile?agent_id=_default",
        json={"security_profile": "standard", "overrides": {}},
    )
    client.put(
        "/v1/admin/firewall/profile?agent_id=hr-bot",
        json={"security_profile": "strict", "overrides": {"hideOtherUsersData": True}},
    )
    resp = client.get("/v1/admin/firewall/profile?agent_id=hr-bot")
    body = resp.json()
    assert body["agent_id"] == "hr-bot"
    assert body["security_profile"] == "strict"
    assert body["overrides"]["hideOtherUsersData"] is True


def test_firewall_profile_legacy_profile_aliases_accepted(client: TestClient) -> None:
    """Old name aliases (balanced, permissive, observability) still work."""
    for legacy in ["balanced", "permissive", "observability"]:
        resp = client.put(
            "/v1/admin/firewall/profile?agent_id=_default",
            json={"security_profile": legacy},
        )
        assert resp.status_code == 200, f"legacy alias {legacy!r} rejected: {resp.text}"
