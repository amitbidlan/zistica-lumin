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
    # New defensive-restore semantics: the import happens in a fresh
    # temp DuckDB. If THAT fails, the live DB is never touched. The
    # error message mentions the pre-restore snapshot for operator
    # confidence even though it shouldn't have been needed.
    detail = body["detail"].lower()
    assert "restore failed" in detail or "import database failed" in detail
    assert "pre-restore" in detail or "pre_restore" in detail

    # CRITICAL ASSERTION: the live DB must still have the original
    # data. With the fresh-connection swap, the live DB is never
    # touched on import failure — the temp DB just gets discarded.
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


# ----- Persistent-DB regression test ---------------------------------------


def test_restore_works_on_persistent_disk_db_after_heavy_use(
    tmp_path: Path, monkeypatch,
) -> None:
    """The bug that motivated PR (round-3): live brutal testing
    found that restore against a persistent on-disk DuckDB hit
    'subject idx_X has been deleted' after the connection had
    accumulated catalog state from many earlier queries.

    This test reproduces the live conditions: a persistent DB,
    many intermediate operations to build catalog state, THEN the
    restore. With the fresh-connection swap, it should work.
    """
    duck_path = tmp_path / "persist.duckdb"
    sqlite_path = tmp_path / "persist.sqlite"
    db = Database(duckdb_path=str(duck_path), sqlite_path=str(sqlite_path))
    monkeypatch.setenv("LUMIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LUMIN_BACKUP_DIR", str(tmp_path / "backups"))

    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        client = TestClient(main.app)

        # Seed real data. Then do MANY ops that build catalog state.
        _seed(db, 50)
        # Insert spans, decisions, vault entries — exercise the
        # whole table set so DROP+IMPORT would have to deal with
        # all the indexes.
        for i in range(20):
            db.execute(
                "INSERT INTO spans (id, trace_id, started_at, type) "
                "VALUES (?, ?, ?, ?)",
                [f"sp-{i}", f"orig-{i}", "2026-01-01 00:00:00", "llm"],
            )
            db.execute(
                """
                INSERT INTO decisions (
                    id, policy_id, policy_name, lifecycle, decision,
                    mode_at_decision, decision_at, duration_ms
                ) VALUES (?, 'p', 'p', 'before_proxy_call', 'block',
                          'enforce', ?, 1)
                """,
                [f"dec-{i}", "2026-01-01 00:00:00"],
            )

        # Snapshot
        create = client.post("/v1/admin/backups", json={"name": "snap"})
        assert create.status_code == 200, create.text

        # Mutate
        db.execute(
            "INSERT INTO traces (id, name, started_at) "
            "VALUES ('post', 'NEW', '2026-01-02 00:00:00')",
        )

        # Restore — this is the path that wedged in the original bug
        resp = client.post(
            "/v1/admin/backups/snap/restore", json={"confirm": True},
        )
        assert resp.status_code == 200, resp.text

        # After the swap, read via the live API. If the swap worked
        # the live db connection points at the restored file.
        resp_traces = client.get("/v1/traces?limit=100")
        assert resp_traces.status_code == 200
        ids = {t["id"] for t in resp_traces.json()}
        assert "post" not in ids
        assert any(i.startswith("orig-") for i in ids)
    finally:
        main.app.dependency_overrides.clear()
        db.close()
