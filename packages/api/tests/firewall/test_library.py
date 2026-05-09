"""Tests for the starter-pack library (§13 / Slice 4).

Coverage:

  - Every YAML in firewall/starter_packs/ parses without a
    PolicyConfigError. A typo in any pack fails CI here, before
    operators see a 500.
  - list_packs() surfaces every pack with metadata
  - preview_pack() returns the policy list without writing to DB
  - import_pack() writes policies, defaults to shadow mode, is
    idempotent on duplicate names
  - Path-traversal / invalid pack_id is rejected
  - HTTP endpoints respond with the right shape
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from db import Database
from firewall import library as fw_library
import main
import policy_store


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture
def client(db: Database):
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# ----- pack files parse cleanly -------------------------------------------


def test_every_pack_parses_without_error() -> None:
    """Smoke: every pack YAML on disk loads. Catches typos / DSL
    errors before any test that depends on a specific pack fires."""
    packs = fw_library.list_packs()
    assert len(packs) >= 11, (
        f"expected at least 11 packs (Slice 4 ships 10 + 1 OWASP), "
        f"found {len(packs)}"
    )


def test_every_pack_ships_shadow_default() -> None:
    """§10.1 — every starter pack must declare mode=shadow on every
    policy. Operator promotes per environment after observing
    EnforcementTimeline."""
    packs = fw_library.list_packs()
    for pack in packs:
        preview = fw_library.preview_pack(pack["pack_id"])
        non_shadow = [
            p["name"] for p in preview["policies"]
            if p.get("mode") != "shadow"
        ]
        assert non_shadow == [], (
            f"pack {pack['pack_id']!r} has non-shadow rules: {non_shadow}"
        )


# ----- list_packs ---------------------------------------------------------


def test_list_packs_returns_metadata() -> None:
    packs = fw_library.list_packs()
    by_id = {p["pack_id"]: p for p in packs}
    # Hand-pick a few we know ship in Slice 4
    assert "owasp_llm_top_10" in by_id
    assert "owasp_agentic_2025" in by_id
    assert "compliance_gdpr" in by_id
    assert "cost_guards" in by_id
    pack = by_id["compliance_gdpr"]
    assert pack["category"] == "Compliance"
    assert pack["policy_count"] >= 1
    assert "description" in pack and len(pack["description"]) > 0
    assert isinstance(pack["lifecycles"], list)


def test_list_packs_marks_owasp_auto_installed() -> None:
    packs = fw_library.list_packs()
    by_id = {p["pack_id"]: p for p in packs}
    assert by_id["owasp_llm_top_10"]["auto_installed"] is True
    # Other packs are operator-installed
    assert by_id["compliance_gdpr"]["auto_installed"] is False


# ----- preview_pack -------------------------------------------------------


def test_preview_pack_returns_policies() -> None:
    preview = fw_library.preview_pack("compliance_pci_dss")
    assert preview["pack_id"] == "compliance_pci_dss"
    assert preview["policy_count"] >= 1
    assert len(preview["policies"]) == preview["policy_count"]
    first = preview["policies"][0]
    # Each row has the dashboard's expected shape
    for key in ("name", "lifecycle", "mode", "condition", "action", "severity"):
        assert key in first


def test_preview_unknown_pack_raises() -> None:
    with pytest.raises(FileNotFoundError):
        fw_library.preview_pack("does_not_exist")


def test_preview_traversal_rejected() -> None:
    """Defense in depth: a malicious pack_id with .. or / is
    rejected before touching the filesystem."""
    with pytest.raises((FileNotFoundError, ValueError)):
        fw_library.preview_pack("../../etc/passwd")


# ----- import_pack --------------------------------------------------------


def test_import_pack_writes_policies(db: Database) -> None:
    result = fw_library.import_pack(db, "cost_guards")
    assert result.imported > 0
    assert result.failed == 0
    assert result.skipped_duplicates == 0
    rows = policy_store.list_policies(db)
    assert len(rows) == result.imported


def test_import_pack_idempotent(db: Database) -> None:
    """Re-importing the same pack skips duplicates, preserves
    operator edits to existing rows."""
    first = fw_library.import_pack(db, "cost_guards")
    second = fw_library.import_pack(db, "cost_guards")
    assert first.imported > 0
    assert second.imported == 0
    assert second.skipped_duplicates == first.imported


def test_import_pack_lands_in_shadow(db: Database) -> None:
    """§10.1 — even if a pack mistakenly declared mode=enforce, the
    import normalises to shadow."""
    fw_library.import_pack(db, "owasp_agentic_2025")
    rows = policy_store.list_policies(db)
    assert all(p.mode == "shadow" for p in rows), (
        f"non-shadow rows after import: "
        f"{[p.name for p in rows if p.mode != 'shadow']}"
    )


def test_import_unknown_pack_raises(db: Database) -> None:
    with pytest.raises(FileNotFoundError):
        fw_library.import_pack(db, "no_such_pack")


# ----- HTTP surface -------------------------------------------------------


def test_get_library_lists_packs(client: TestClient) -> None:
    resp = client.get("/v1/firewall/library")
    assert resp.status_code == 200
    body = resp.json()
    assert "packs" in body
    pack_ids = {p["pack_id"] for p in body["packs"]}
    # Every Slice 4 pack must show up
    expected = {
        "owasp_llm_top_10",
        "owasp_agentic_2025",
        "dev_environment_safety",
        "customer_support_agent",
        "code_assistant",
        "compliance_gdpr",
        "compliance_hipaa",
        "compliance_pci_dss",
        "framework_mastra",
        "framework_langgraph",
        "cost_guards",
    }
    missing = expected - pack_ids
    assert missing == set(), f"missing packs in /v1/firewall/library: {missing}"


def test_get_library_preview(client: TestClient) -> None:
    resp = client.get("/v1/firewall/library/customer_support_agent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pack_id"] == "customer_support_agent"
    assert body["policy_count"] >= 1
    assert isinstance(body["policies"], list)


def test_get_library_preview_404_for_unknown(client: TestClient) -> None:
    resp = client.get("/v1/firewall/library/no_such_pack")
    assert resp.status_code == 404


def test_post_library_import_writes_rows(client: TestClient, db: Database) -> None:
    resp = client.post("/v1/firewall/library/dev_environment_safety/import")
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] > 0
    assert body["failed"] == 0
    rows = policy_store.list_policies(db)
    names = {r.name for r in rows}
    assert any(n.startswith("dev_safety_") for n in names)


def test_post_library_import_unknown_pack_404(client: TestClient) -> None:
    resp = client.post("/v1/firewall/library/no_such_pack/import")
    assert resp.status_code == 404
