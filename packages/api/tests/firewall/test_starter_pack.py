"""Tests for the OWASP LLM Top 10 starter pack auto-install
(§10.1 + §11 of AGENT_FIREWALL_SPEC.md, task #42).

Verifies:

  - Bootstrap imports rules into a fresh DB
  - All imported rules land in mode=shadow (never block live traffic
    on a fresh install)
  - Bootstrap is idempotent — re-running with rules already present
    is a no-op
  - LUMIN_DISABLE_STARTER_PACK env var skips the import
  - LUMIN_POLICY_FILE set defers to user YAML (no starter pack)
  - The 9 imported rules parse cleanly with the firewall builtins —
    a typo in starter_pack/owasp_llm_top_10.yaml fails CI here
"""

from __future__ import annotations

import os

import pytest

from db import Database
from firewall.starter_packs import bootstrap as starter_bootstrap
import policy_store


@pytest.fixture(autouse=True)
def _no_user_yaml(monkeypatch):
    """Strip env so starter pack tests run in a deterministic
    environment regardless of dev shell config."""
    monkeypatch.delenv("LUMIN_POLICY_FILE", raising=False)
    monkeypatch.delenv("LUMIN_DISABLE_STARTER_PACK", raising=False)


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


def test_bootstrap_imports_rules_into_fresh_db(db: Database) -> None:
    n = starter_bootstrap.install_owasp_pack_if_fresh(db)
    assert n > 0, "expected starter pack to import at least one rule"
    rows = policy_store.list_policies(db)
    assert len(rows) == n


def test_all_imported_rules_are_shadow_mode(db: Database) -> None:
    starter_bootstrap.install_owasp_pack_if_fresh(db)
    rows = policy_store.list_policies(db)
    non_shadow = [p for p in rows if p.mode != "shadow"]
    assert non_shadow == [], (
        f"starter pack rules must ship in mode=shadow per §10.1; "
        f"found non-shadow: {[p.name for p in non_shadow]}"
    )


def test_bootstrap_is_idempotent(db: Database) -> None:
    first = starter_bootstrap.install_owasp_pack_if_fresh(db)
    second = starter_bootstrap.install_owasp_pack_if_fresh(db)
    assert first > 0
    assert second == 0  # already populated → skip


def test_disable_env_var_skips_import(db: Database, monkeypatch) -> None:
    monkeypatch.setenv("LUMIN_DISABLE_STARTER_PACK", "true")
    n = starter_bootstrap.install_owasp_pack_if_fresh(db)
    assert n == 0
    assert policy_store.list_policies(db) == []


def test_user_yaml_takes_precedence(db: Database, monkeypatch, tmp_path) -> None:
    """When LUMIN_POLICY_FILE is set, user YAML wins — starter pack
    defers so the user-authored rules aren't shadowed by ours."""
    user_file = tmp_path / "user.yaml"
    user_file.write_text("version: 1\npolicies: []\n")
    monkeypatch.setenv("LUMIN_POLICY_FILE", str(user_file))
    n = starter_bootstrap.install_owasp_pack_if_fresh(db)
    assert n == 0


def test_imported_rules_have_valid_lifecycle_and_mode(db: Database) -> None:
    starter_bootstrap.install_owasp_pack_if_fresh(db)
    rows = policy_store.list_policies(db)
    for p in rows:
        assert p.lifecycle in policy_store.VALID_LIFECYCLES, (
            f"{p.name}: bad lifecycle {p.lifecycle}"
        )
        assert p.mode in policy_store.VALID_MODES, (
            f"{p.name}: bad mode {p.mode}"
        )
        assert p.action in (
            "block", "flag", "require_approval", "rewrite", "allow"
        ), f"{p.name}: action {p.action} not a firewall action"
