"""Tests for the rule template gallery (Slice 2 Tier 1.05).

Covers:
  - Loader parses real YAML templates correctly
  - Malformed YAML is skipped (not fatal)
  - compile_rule renders condition with field-value substitution
  - Multi-select renders as a Python list literal
  - select with custom ``value`` mapping uses the mapped value
  - Field validation rejects choices not in ``choices``
  - Default mode is shadow (§10.1)
  - HTTP endpoints: list, detail, instantiate
  - Created policy lands in DB with the compiled condition
  - Name conflict surfaces as 409
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db import Database
from firewall.templates import loader as fw_templates


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _reload_real_templates():
    """Each test gets a fresh load of the production templates dir.
    Tests that swap in a fixture directory call reload_templates(...)
    explicitly."""
    fw_templates.reload_templates()
    yield
    fw_templates.reload_templates()


# ---- loader -------------------------------------------------------------


def test_loader_picks_up_destructive_shell():
    tpls = fw_templates.load_templates()
    assert "destructive_shell" in tpls
    tpl = tpls["destructive_shell"]
    assert tpl["name"] == "Block destructive shell commands"
    assert tpl["category"] == "filesystem"
    assert any(f["id"] == "tools" for f in tpl["fields"])


def test_loader_picks_up_sensitive_file_reads():
    tpls = fw_templates.load_templates()
    assert "sensitive_file_reads" in tpls


def test_loader_skips_malformed_yaml(tmp_path):
    (tmp_path / "good.yaml").write_text(
        "id: ok\nname: Good\nsummary: ok\nfields: []\ndefaults: {}\ncondition: 'True'\ndescription: ok\n"
    )
    (tmp_path / "broken.yaml").write_text("not: valid: yaml: at: all: : :")
    (tmp_path / "no_id.yaml").write_text("name: missing id\n")
    out = fw_templates.reload_templates(tmp_path)
    assert "ok" in out
    assert len(out) == 1


def test_get_template_returns_none_for_unknown():
    assert fw_templates.get_template("does_not_exist") is None


def test_list_summary_drops_heavy_fields():
    summaries = fw_templates.list_templates_summary()
    assert len(summaries) >= 2
    for s in summaries:
        assert {"id", "name", "icon", "summary", "category", "field_count"} <= set(s.keys())
        assert "condition" not in s
        assert "fields" not in s


# ---- compile_rule -------------------------------------------------------


def test_compile_rule_substitutes_multi_select():
    pol = fw_templates.compile_rule(
        "destructive_shell",
        name="my_block_destruct",
        field_values={"tools": ["exec", "shell"], "action": "block"},
    )
    # Multi-select renders as Python list literal
    assert "['exec', 'shell']" in pol.condition
    assert pol.action == "block"
    assert pol.lifecycle == "before_tool_call"
    assert pol.severity == "critical"
    assert pol.priority == 100


def test_compile_rule_uses_field_default_when_missing():
    """If the operator omits a field, fall back to the field's default."""
    pol = fw_templates.compile_rule(
        "destructive_shell",
        name="defaults_test",
        field_values={},  # nothing supplied
    )
    # Default tools list contains 'exec' — should be in the condition
    assert "exec" in pol.condition


def test_compile_rule_select_with_value_mapping():
    """The sensitive_file_reads template uses select.value to map a
    short id (e.g. 'ssh_only') onto a full regex string. compile_rule
    should substitute the mapped value, not the id."""
    pol = fw_templates.compile_rule(
        "sensitive_file_reads",
        name="ssh_block",
        field_values={"tools": ["exec"], "paths_pattern": "ssh_only", "action": "block"},
    )
    # Mapped value contains "~/.ssh/" — id "ssh_only" should NOT appear
    assert "~/.ssh/" in pol.condition
    assert "ssh_only" not in pol.condition


def test_compile_rule_rejects_invalid_choice():
    with pytest.raises(ValueError, match="not in"):
        fw_templates.compile_rule(
            "destructive_shell",
            name="bad",
            field_values={"tools": ["not_a_real_shell"], "action": "block"},
        )


def test_compile_rule_unknown_template_raises():
    with pytest.raises(KeyError, match="unknown template"):
        fw_templates.compile_rule("does_not_exist", name="x", field_values={})


def test_compile_rule_defaults_to_shadow_mode():
    pol = fw_templates.compile_rule(
        "destructive_shell",
        name="mode_default",
        field_values={"tools": ["exec"]},
    )
    assert pol.mode == "shadow"


def test_compile_rule_mode_override():
    pol = fw_templates.compile_rule(
        "destructive_shell",
        name="enforce_now",
        field_values={"tools": ["exec"]},
        mode="enforce",
    )
    assert pol.mode == "enforce"


# ---- HTTP endpoints -----------------------------------------------------


def test_list_templates_endpoint(client):
    r = client.get("/v1/firewall/templates")
    assert r.status_code == 200
    body = r.json()
    ids = {t["id"] for t in body["templates"]}
    assert "destructive_shell" in ids
    assert "sensitive_file_reads" in ids


def test_get_template_detail_endpoint(client):
    r = client.get("/v1/firewall/templates/destructive_shell")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "destructive_shell"
    assert "fields" in body
    assert "condition" in body


def test_get_template_detail_404(client):
    r = client.get("/v1/firewall/templates/not_a_real_template")
    assert r.status_code == 404


def test_instantiate_creates_policy(client, db):
    r = client.post(
        "/v1/firewall/templates/destructive_shell/instantiate",
        json={
            "name": "my_destruct_rule",
            "field_values": {"tools": ["exec", "bash"], "action": "block"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "my_destruct_rule"
    assert body["mode"] == "shadow"
    assert "['exec', 'bash']" in body["condition"]

    # Policy actually exists in DB
    rows = db.fetchall_dict(
        "SELECT name, mode, lifecycle FROM policies WHERE name = ?",
        ["my_destruct_rule"],
    )
    assert len(rows) == 1
    assert rows[0]["mode"] == "shadow"
    assert rows[0]["lifecycle"] == "before_tool_call"


def test_instantiate_name_conflict_409(client, db):
    body = {
        "name": "duplicate_name",
        "field_values": {"tools": ["exec"], "action": "block"},
    }
    r1 = client.post("/v1/firewall/templates/destructive_shell/instantiate", json=body)
    assert r1.status_code == 200
    r2 = client.post("/v1/firewall/templates/destructive_shell/instantiate", json=body)
    assert r2.status_code == 409


def test_instantiate_400_on_invalid_choice(client):
    r = client.post(
        "/v1/firewall/templates/destructive_shell/instantiate",
        json={
            "name": "bad_choice",
            "field_values": {"tools": ["not_real"], "action": "block"},
        },
    )
    assert r.status_code == 400


def test_instantiate_404_on_unknown_template(client):
    r = client.post(
        "/v1/firewall/templates/no_such_template/instantiate",
        json={"name": "x", "field_values": {}},
    )
    assert r.status_code == 404
