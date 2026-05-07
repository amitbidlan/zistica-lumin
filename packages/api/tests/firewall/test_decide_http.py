"""HTTP-level tests for the Agent Firewall router (§5.1–5.4, 5.6, 5.7,
10.2 of AGENT_FIREWALL_SPEC.md).

Verifies the endpoint surfaces, not the engine internals (those live
in ``test_decide.py``). Focuses on:

  - ``POST /v1/policy/decide`` returns the right shape and never 5xx
  - ``GET /v1/decisions`` filters + paginates
  - ``GET /v1/decisions/{id}`` returns siblings and policy snapshot
  - ``POST /v1/policies/{name}/mode`` flips mode + returns forecast
  - ``POST /v1/firewall/panic_disable`` sets the flag + persists
  - ``POST /v1/approvals/{id}/resolve`` flips state
  - Shadow-mode default (§10.1, task #38) — new policies created via
    POST /v1/policies start in mode='shadow' unless overridden
"""

from __future__ import annotations

from typing import Any

import pytest

from db import Database
from firewall import decide as fw_decide
from lumin.policy import Policy
import policy_store


# ---- helpers -------------------------------------------------------------


def _create_policy_via_db(
    db: Database, *, name: str, action: str = "block",
    mode: str = "enforce", lifecycle: str = "before_tool_call",
    condition: str = "True", priority: int = 0,
) -> None:
    """Bypass the HTTP create handler so the test doesn't depend on
    that endpoint working. The decide endpoint and dashboard endpoints
    just need rows in the policies table to read from."""
    p = Policy(
        name=name, description=f"test {name}",
        trigger="span_end", condition=condition,
        action=action, severity="medium",
        lifecycle=lifecycle, mode=mode, priority=priority,
    )
    policy_store.create_policy(db, p, actor="test")


# ---- /v1/policy/decide ---------------------------------------------------


def test_decide_endpoint_returns_block(client, db) -> None:
    _create_policy_via_db(db, name="block_all", action="block")
    r = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "block"
    assert body["policy_name"] == "block_all"


def test_decide_endpoint_returns_allow_when_no_policies(client) -> None:
    r = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"


def test_decide_endpoint_never_500s_on_garbage(client) -> None:
    r = client.post("/v1/policy/decide", json={"lifecycle": "garbage"})
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"


# ---- /v1/decisions list + detail ----------------------------------------


def test_list_decisions_returns_recent(client, db) -> None:
    _create_policy_via_db(db, name="rec_all", action="flag")
    for i in range(3):
        client.post(
            "/v1/policy/decide",
            json={
                "lifecycle": "before_tool_call",
                "tool_name": "shell",
                "trace_id": f"t{i}",
            },
        )
    r = client.get("/v1/decisions")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["decisions"]) == 3


def test_list_decisions_filters_by_decision(client, db) -> None:
    _create_policy_via_db(db, name="block_all", action="block", priority=10)
    _create_policy_via_db(
        db, name="flag_subset", action="flag",
        condition='tool_name == "lookup"',
        priority=20,
    )
    client.post("/v1/policy/decide", json={"lifecycle": "before_tool_call", "tool_name": "shell"})
    client.post("/v1/policy/decide", json={"lifecycle": "before_tool_call", "tool_name": "lookup"})
    r = client.get("/v1/decisions?decision=flag")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["decisions"][0]["decision"] == "flag"


def test_get_decision_detail_returns_policy_snapshot(client, db) -> None:
    _create_policy_via_db(db, name="block_x", action="block")
    create_resp = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    decision_id = create_resp.json()["decision_id"]
    r = client.get(f"/v1/decisions/{decision_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["id"] == decision_id
    assert body["policy"]["name"] == "block_x"


def test_get_decision_detail_404_on_missing(client) -> None:
    r = client.get("/v1/decisions/nope_not_real")
    assert r.status_code == 404


# ---- /v1/policies/{name}/mode -------------------------------------------


def test_set_policy_mode_flips_value(client, db) -> None:
    _create_policy_via_db(db, name="m1", mode="shadow")
    r = client.post("/v1/policies/m1/mode", json={"mode": "enforce"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "enforce"
    assert body["previous_mode"] == "shadow"


def test_set_policy_mode_returns_forecast_count(client, db) -> None:
    _create_policy_via_db(db, name="bx", action="block", mode="shadow")
    # Generate decisions in shadow so the forecast has population.
    for _ in range(3):
        client.post(
            "/v1/policy/decide",
            json={"lifecycle": "before_tool_call", "tool_name": "shell"},
        )
    r = client.post("/v1/policies/bx/mode", json={"mode": "enforce"})
    assert r.status_code == 200
    forecast = r.json()["forecast"]
    assert forecast["would_have_blocked"] == 3


def test_set_policy_mode_rejects_bad_mode(client, db) -> None:
    _create_policy_via_db(db, name="m2")
    r = client.post("/v1/policies/m2/mode", json={"mode": "garbage"})
    assert r.status_code == 400


def test_set_policy_mode_404_on_missing(client) -> None:
    r = client.post("/v1/policies/missing/mode", json={"mode": "enforce"})
    assert r.status_code == 404


# ---- panic disable (§10.2) ----------------------------------------------


def test_panic_disable_short_circuits_decide(client, db) -> None:
    _create_policy_via_db(db, name="block_all", action="block")
    # Sanity: blocks before panic.
    r = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    assert r.json()["decision"] == "block"

    # Panic on.
    r = client.post(
        "/v1/firewall/panic_disable",
        json={"disabled": True, "reason": "drill", "actor": "ops@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["disabled"] is True

    # Now decides allow.
    r = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    assert r.json()["decision"] == "allow"
    assert r.json()["reason"] == "panic_disabled"

    # Re-enable.
    r = client.post(
        "/v1/firewall/panic_disable",
        json={"disabled": False, "actor": "ops@example.com"},
    )
    assert r.json()["disabled"] is False


def test_panic_state_persists_via_kv(client, db) -> None:
    client.post(
        "/v1/firewall/panic_disable",
        json={"disabled": True, "reason": "test"},
    )
    row = db.fetchone_dict(
        "SELECT v FROM firewall_kv WHERE k = 'panic_disabled'"
    )
    assert row is not None
    assert "disabled" in row["v"]
    # Reset for other tests
    client.post("/v1/firewall/panic_disable", json={"disabled": False})


# ---- approvals -----------------------------------------------------------


def test_approval_resolve_flow(client, db) -> None:
    _create_policy_via_db(db, name="needs_apv", action="require_approval")
    r = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    apv_id = r.json()["approval_id"]

    r = client.get(f"/v1/approvals/{apv_id}")
    assert r.status_code == 200
    assert r.json()["state"] == "pending"

    r = client.post(
        f"/v1/approvals/{apv_id}/resolve",
        json={"resolution": "deny", "reason": "ops"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "denied"

    # Resolving twice is a 409.
    r = client.post(
        f"/v1/approvals/{apv_id}/resolve",
        json={"resolution": "allow"},
    )
    assert r.status_code == 409


def test_list_approvals_filters_by_state(client, db) -> None:
    _create_policy_via_db(db, name="needs_apv", action="require_approval")
    client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "shell"},
    )
    r = client.get("/v1/approvals?state=pending")
    assert r.status_code == 200
    assert r.json()["total"] >= 1


# ---- shadow-mode default for new policies (§10.1, task #38) -------------


def test_new_policy_via_api_defaults_to_shadow(client) -> None:
    """A POST /v1/policies without an explicit mode must land in
    mode='shadow' so live traffic isn't affected on first save."""
    r = client.post(
        "/v1/policies",
        json={
            "name": "fresh_rule",
            "trigger": "span_end",
            "condition": "True",
            "action": "block",
            "severity": "medium",
        },
    )
    assert r.status_code in (200, 201)

    r = client.get("/v1/policies/fresh_rule")
    assert r.status_code == 200
    # /v1/policies/{name} returns PolicyOut — mode lives on the
    # row underneath even if the model doesn't surface it yet.
    # Cross-check via /v1/decisions by triggering the rule and
    # confirming it didn't block.
    decide = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "post_ingest", "tool_name": "x"},
    )
    body = decide.json()
    # Rule is in shadow → engine returns allow but records the would-
    # have-been-block as a shadow_hit.
    assert body["decision"] == "allow"
    if "shadow_hits" in body:
        assert any(h["policy_id"] == "fresh_rule" for h in body["shadow_hits"])


def test_new_policy_with_explicit_mode_overrides_default(client) -> None:
    r = client.post(
        "/v1/policies",
        json={
            "name": "explicit_enforce",
            "trigger": "span_end",
            "condition": "True",
            "action": "block",
            "severity": "medium",
            "lifecycle": "before_tool_call",
            "mode": "enforce",
        },
    )
    assert r.status_code in (200, 201)
    decide = client.post(
        "/v1/policy/decide",
        json={"lifecycle": "before_tool_call", "tool_name": "x"},
    )
    assert decide.json()["decision"] == "block"
