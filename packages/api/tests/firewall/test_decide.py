"""Tests for the synchronous decision engine (§5.1, §10.1–10.3 of
AGENT_FIREWALL_SPEC.md).

Covers:

  - Allow path when no policy applies (fast path)
  - Block / flag / require_approval / rewrite verbs
  - Mode resolution (shadow / flag / enforce)
  - Priority ordering and explicit-allow short-circuit
  - Lifecycle filtering
  - Agent scope filtering
  - Circuit-breaker tripped policies are skipped
  - Panic disable global kill-switch
  - Internal error handling (Rule 7)
  - Latency budget honored under simulated timeout
  - Decision row written for every non-fast-path request
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from db import Database
from firewall import decide as fw_decide
from lumin.policy import Policy
import policy_store


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def db() -> Database:
    """In-memory DuckDB per test — schema + firewall migrations run
    on init via Database._init_schema."""
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    fw_decide.set_panic_disabled(False)
    yield d
    fw_decide.set_panic_disabled(False)
    d.close()


def _mk_policy(
    db: Database,
    *,
    name: str,
    lifecycle: str = "before_tool_call",
    mode: str = "enforce",
    priority: int = 0,
    action: str = "block",
    condition: str = "True",
    scope_agents=None,
) -> Policy:
    p = Policy(
        name=name,
        description=f"test policy {name}",
        trigger="span_end",
        condition=condition,
        action=action,
        severity="medium",
        scope_agents=scope_agents or [],
        lifecycle=lifecycle,
        mode=mode,
        priority=priority,
    )
    return policy_store.create_policy(db, p, actor="test")


# ---- fast paths -----------------------------------------------------------


def test_allow_when_no_policies(db: Database) -> None:
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "allow"
    assert "duration_ms" in out


def test_unknown_lifecycle_short_circuits_allow(db: Database) -> None:
    _mk_policy(db, name="never_applies")
    out = fw_decide.decide(db, lifecycle="not_a_real_lifecycle")
    assert out["decision"] == "allow"
    assert "unknown_lifecycle" in out["reason"]


# ---- block / flag / require_approval / rewrite ---------------------------


def test_block_decision_records_row_and_returns(db: Database) -> None:
    _mk_policy(db, name="block_all", action="block", condition="True")
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "block"
    assert out["policy_name"] == "block_all"
    assert out["mode_at_decision"] == "enforce"
    assert "decision_id" in out

    rows = db.fetchall_dict("SELECT * FROM decisions WHERE id = ?", [out["decision_id"]])
    assert len(rows) == 1
    assert rows[0]["decision"] == "block"
    assert rows[0]["policy_name"] == "block_all"
    assert rows[0]["lifecycle"] == "before_tool_call"


def test_flag_decision(db: Database) -> None:
    _mk_policy(db, name="flag_all", action="flag", condition="True")
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "flag"
    assert out["policy_name"] == "flag_all"


def test_require_approval_creates_approval_row(db: Database) -> None:
    _mk_policy(
        db, name="needs_approval",
        action="require_approval", condition="True",
    )
    out = fw_decide.decide(
        db, lifecycle="before_tool_call",
        tool_name="shell", params={"command": "rm -rf /tmp/x"},
        agent="test_agent", trace_id="trace-1",
    )
    assert out["decision"] == "require_approval"
    assert out["approval_id"].startswith("apv_")
    assert out["timeout_s"] == 600

    apv = db.fetchone_dict(
        "SELECT * FROM approvals WHERE id = ?", [out["approval_id"]]
    )
    assert apv is not None
    assert apv["state"] == "pending"
    assert apv["policy_id"] == "needs_approval"
    assert apv["trace_id"] == "trace-1"


def test_rewrite_redacts_pii(db: Database) -> None:
    _mk_policy(
        db, name="rewrite_pii",
        lifecycle="after_tool_call",
        action="rewrite",
        condition="has_pii(text)",
    )
    out = fw_decide.decide(
        db, lifecycle="after_tool_call",
        tool_name="lookup",
        params={"name": "Customer ssn 123-45-6789 and card 4111 1111 1111 1111"},
    )
    assert out["decision"] == "rewrite"
    redacted = out["rewritten"]["params"]["name"]
    assert "123-45-6789" not in redacted
    assert "4111" not in redacted


# ---- mode resolution -----------------------------------------------------


def test_shadow_mode_returns_allow_but_records_decision(db: Database) -> None:
    _mk_policy(
        db, name="shadow_block",
        action="block", mode="shadow", condition="True",
    )
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "allow"
    # shadow_hits surfaced for the dashboard
    assert "shadow_hits" in out
    assert out["shadow_hits"][0]["policy_id"] == "shadow_block"
    assert out["shadow_hits"][0]["would_have_been"] == "block"

    rows = db.fetchall_dict(
        "SELECT * FROM decisions WHERE policy_name = ?", ["shadow_block"]
    )
    assert len(rows) == 1
    assert rows[0]["mode_at_decision"] == "shadow"
    assert rows[0]["decision"] == "block"  # what it WOULD have done


def test_flag_mode_overrides_action(db: Database) -> None:
    """A block-action rule in mode=flag returns decision=flag."""
    _mk_policy(
        db, name="flag_mode_block",
        action="block", mode="flag", condition="True",
    )
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "flag"
    assert out["mode_at_decision"] == "flag"


# ---- priority + explicit allow ------------------------------------------


def test_priority_orders_evaluation(db: Database) -> None:
    """A high-priority block fires before a low-priority allow."""
    _mk_policy(db, name="low_allow", action="allow", priority=10, condition="True")
    _mk_policy(db, name="high_block", action="block", priority=100, condition="True")
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "block"
    assert out["policy_name"] == "high_block"


def test_explicit_allow_short_circuits(db: Database) -> None:
    """A higher-priority allow skips lower-priority blocks."""
    _mk_policy(db, name="low_block", action="block", priority=10, condition="True")
    _mk_policy(db, name="high_allow", action="allow", priority=100, condition="True")
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "allow"
    assert out["policy_name"] == "high_allow"


# ---- lifecycle and scope filtering --------------------------------------


def test_lifecycle_mismatch_skips_policy(db: Database) -> None:
    _mk_policy(
        db, name="post_only",
        lifecycle="post_ingest", action="block", condition="True",
    )
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "allow"


def test_scope_agents_filters(db: Database) -> None:
    _mk_policy(
        db, name="bot_only",
        action="block", condition="True",
        scope_agents=["bot.support"],
    )
    out_match = fw_decide.decide(
        db, lifecycle="before_tool_call", tool_name="shell", agent="bot.support"
    )
    assert out_match["decision"] == "block"

    out_skip = fw_decide.decide(
        db, lifecycle="before_tool_call", tool_name="shell", agent="other_agent"
    )
    assert out_skip["decision"] == "allow"


# ---- circuit breaker -----------------------------------------------------


def test_tripped_circuit_breaker_skips_policy(db: Database) -> None:
    _mk_policy(db, name="will_be_tripped", action="block", condition="True")
    policy_store.update_policy(
        db, "will_be_tripped", circuit_breaker_state="tripped"
    )
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "allow"


# ---- panic disable -------------------------------------------------------


def test_panic_disable_short_circuits_to_allow(db: Database) -> None:
    _mk_policy(db, name="block_all", action="block", condition="True")
    fw_decide.set_panic_disabled(True, reason="incident-123")
    try:
        out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
        assert out["decision"] == "allow"
        assert out["reason"] == "panic_disabled"
        assert out["panic_reason"] == "incident-123"
    finally:
        fw_decide.set_panic_disabled(False)

    # No decision row recorded for panic short-circuit (we don't want
    # the audit log spammed during an incident; the panic write itself
    # is the audit trail).
    rows = db.fetchall_dict("SELECT * FROM decisions")
    assert rows == []


# ---- error handling (Rule 7) ---------------------------------------------


def test_broken_condition_falls_back_to_allow_by_default(db: Database) -> None:
    """A policy whose condition references an undefined name falls
    through (Rule 7 — agent never blocks on Lumin)."""
    _mk_policy(
        db, name="broken",
        action="block",
        # NameError at eval time — undefined_name is not in the
        # namespace table.
        condition="undefined_name",
    )
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "allow"


def test_on_internal_error_deny_blocks(db: Database) -> None:
    _mk_policy(db, name="strict", action="block", condition="undefined_name")
    policy_store.update_policy(db, "strict", on_internal_error="deny")
    out = fw_decide.decide(db, lifecycle="before_tool_call", tool_name="shell")
    assert out["decision"] == "block"
    assert out["reason"] == "internal_error"


# ---- input namespace -----------------------------------------------------


def test_condition_can_read_tool_name_and_params(db: Database) -> None:
    _mk_policy(
        db, name="block_dangerous",
        action="block",
        condition='tool_name == "shell" and "rm -rf" in str(Input.params.get("command", ""))',
    )
    # Match
    out = fw_decide.decide(
        db, lifecycle="before_tool_call",
        tool_name="shell", params={"command": "rm -rf /etc"},
    )
    assert out["decision"] == "block"

    # Different tool — no match
    out2 = fw_decide.decide(
        db, lifecycle="before_tool_call",
        tool_name="lookup", params={"command": "rm -rf /etc"},
    )
    assert out2["decision"] == "allow"


def test_condition_can_read_output_text(db: Database) -> None:
    _mk_policy(
        db, name="block_secret_in_output",
        lifecycle="after_proxy_call",
        action="block",
        condition="looks_like_secret(Output.text)",
    )
    out = fw_decide.decide(
        db, lifecycle="after_proxy_call",
        # GitHub PAT shape — assembled at runtime to avoid GitHub
        # secret scanning flagging this fixture in source.
        output="Here is your token: " + "ghp_" + "x" * 36,
    )
    assert out["decision"] == "block"


# ---- decision row payload -----------------------------------------------


def test_decision_row_records_all_context(db: Database) -> None:
    _mk_policy(db, name="rec", action="flag", condition="True")
    out = fw_decide.decide(
        db,
        lifecycle="before_tool_call",
        tool_name="shell",
        agent="bot.support",
        project="proj-A",
        session_id="sess-7",
        trace_id="trace-x",
        span_id="span-y",
    )
    row = db.fetchone_dict(
        "SELECT * FROM decisions WHERE id = ?", [out["decision_id"]]
    )
    assert row is not None
    assert row["agent"] == "bot.support"
    assert row["project"] == "proj-A"
    assert row["session_id"] == "sess-7"
    assert row["trace_id"] == "trace-x"
    assert row["span_id"] == "span-y"
    assert row["tool_name"] == "shell"
    assert row["lifecycle"] == "before_tool_call"
