"""Backup-restore transaction-safety tests (brutal-test fix —
verifies the bug found while attacking the API on 2026-05-09).

Before this fix, a corrupted snapshot would leave the live DB in
a half-restored state — some tables dropped, some half-imported,
no rollback. Now the restore handler:

  1. Snapshots the current live DB to ``pre_restore_<UTC ts>``
  2. Drops + IMPORT DATABASE from the operator's chosen snapshot
  3. On failure, re-imports from the pre_restore snapshot

The new test below corrupts the snapshot dir and verifies the
restore endpoint either succeeds OR rolls forward to the
pre-restore state — never wedges the DB.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
import main


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    duck = tmp_path / "rs.duckdb"
    sqlite = tmp_path / "rs.sqlite"
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


def _seed(db: Database, n: int) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO traces (id, name, started_at) VALUES (?, ?, ?)",
            [f"orig-{i}", "agent", "2026-01-01 00:00:00"],
        )


# ----- happy-path round-trip is unchanged ----------------------------------


def test_restore_round_trip_still_works(client: TestClient, db: Database) -> None:
    _seed(db, 5)
    create = client.post("/v1/admin/backups", json={"name": "snap"})
    assert create.status_code == 200, create.text

    db.execute(
        "INSERT INTO traces (id, name, started_at) VALUES ('post', 'NEW', '2026-01-02 00:00:00')",
    )
    assert db.fetchone("SELECT COUNT(*) FROM traces")[0] == 6

    resp = client.post("/v1/admin/backups/snap/restore", json={"confirm": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restored"] == "snap"
    # New: pre_restore_snapshot is reported in the success response
    # so operators can re-restore if they regret.
    assert "pre_restore_snapshot" in body
    assert body["pre_restore_snapshot"].startswith("pre_restore_")

    # State reverted
    assert db.fetchone("SELECT COUNT(*) FROM traces")[0] == 5
    assert db.fetchone("SELECT 1 FROM traces WHERE id='post'") is None


# ----- pre-restore snapshot is created BEFORE the destructive operation ---


def test_pre_restore_snapshot_created(
    client: TestClient, db: Database, tmp_path: Path,
) -> None:
    _seed(db, 3)
    client.post("/v1/admin/backups", json={"name": "snap"})

    backups_before = client.get("/v1/admin/backups").json()["backups"]
    names_before = {b["name"] for b in backups_before}

    client.post("/v1/admin/backups/snap/restore", json={"confirm": True})

    backups_after = client.get("/v1/admin/backups").json()["backups"]
    names_after = {b["name"] for b in backups_after}
    new = names_after - names_before
    assert any(n.startswith("pre_restore_") for n in new), (
        f"expected a pre_restore_* snapshot, got new={new}"
    )


# ----- the brutal test that found the bug ---------------------------------


def test_corrupted_snapshot_does_not_wedge_db(
    client: TestClient, db: Database, tmp_path: Path,
) -> None:
    """Delete one CSV from the snapshot, attempt restore. Even
    though IMPORT will fail mid-way, the live DB must NOT be left
    in a half-restored state — it should either fully restore from
    the operator's snapshot OR roll forward to the pre-restore
    auto-snapshot."""
    _seed(db, 5)
    client.post("/v1/admin/backups", json={"name": "good_snap"})

    # Corrupt the snapshot
    backup_dir = tmp_path / "backups" / "good_snap"
    csvs = sorted(backup_dir.glob("*.csv"))
    assert len(csvs) > 0
    # Pick one to delete that breaks the import. firewall_kv is
    # innocuous enough that the DB still loads if it's missing — but
    # the IMPORT itself will fail. Pick that one for a deterministic
    # test.
    target_csv = next(
        (c for c in csvs if "firewall_kv" in c.name),
        csvs[0],
    )
    target_csv.unlink()

    resp = client.post(
        "/v1/admin/backups/good_snap/restore", json={"confirm": True},
    )
    # Restore is expected to fail because the snapshot is corrupted
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert "IMPORT DATABASE failed" in body["detail"]
    # New behavior: the response mentions the pre-restore snapshot
    # so the operator knows where to recover from.
    assert "pre-restore" in body["detail"].lower() or "pre_restore" in body["detail"]

    # CRITICAL ASSERTION: the live DB must still have the original
    # data (rolled forward to pre-restore state) — NOT half-restored.
    final = db.fetchone("SELECT COUNT(*) FROM traces")
    assert final is not None, "DB is wedged — traces table is gone"
    assert final[0] == 5, (
        f"DB rolled to wrong state: expected 5 traces, got {final[0]}"
    )


def test_restore_unknown_snapshot_doesnt_create_pre_restore(
    client: TestClient, db: Database,
) -> None:
    """A 404 path (unknown snapshot name) shouldn't pollute the
    backup directory with a pre_restore that's never used."""
    _seed(db, 2)
    backups_before = client.get("/v1/admin/backups").json()["backups"]
    resp = client.post(
        "/v1/admin/backups/no_such/restore", json={"confirm": True},
    )
    assert resp.status_code == 404
    backups_after = client.get("/v1/admin/backups").json()["backups"]
    # Same number — no pre_restore snapshot created.
    assert len(backups_after) == len(backups_before)
