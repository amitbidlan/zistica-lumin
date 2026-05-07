"""Tests for Policy DSL extensions (§3 of AGENT_FIREWALL_SPEC.md).

Covers the new firewall fields added to the SDK ``Policy`` dataclass
and round-tripped through ``policy_store``:

  - lifecycle, mode, priority, on_timeout, on_internal_error,
    circuit_breaker_state
  - validation rejects bad enum values at create / update time
  - back-compat: old-format Policy() calls still work with
    sensible defaults
"""

from __future__ import annotations

import pytest
from lumin.policy import Policy

from db import Database
from policy_store import (
    create_policy,
    get_policy,
    update_policy,
    validate_firewall_fields,
)


def _fresh_db() -> Database:
    return Database(duckdb_path=":memory:", sqlite_path=":memory:")


def _basic_policy(**overrides) -> Policy:
    base = dict(
        name="test_policy",
        trigger="span_end",
        condition="True",
        action="log",
        severity="low",
    )
    base.update(overrides)
    return Policy(**base)


# ----- back-compat (legacy Policy() calls) -----


def test_legacy_policy_construction_still_works():
    """A Policy() built with only the original 5 required fields gets
    sensible firewall defaults — back-compat for SDK users on older
    code paths."""
    p = _basic_policy()
    assert p.lifecycle == "post_ingest"
    assert p.mode == "enforce"
    assert p.priority == 0
    assert p.on_timeout == "allow"
    assert p.on_internal_error == "allow"
    assert p.circuit_breaker_state == "ok"


def test_new_policy_with_firewall_fields():
    p = _basic_policy(
        lifecycle="before_tool_call",
        mode="shadow",
        priority=100,
        on_timeout="deny",
    )
    assert p.lifecycle == "before_tool_call"
    assert p.mode == "shadow"
    assert p.priority == 100
    assert p.on_timeout == "deny"


# ----- validation -----


def test_validation_accepts_canonical_values():
    p = _basic_policy(
        lifecycle="before_proxy_call",
        mode="enforce",
        on_timeout="allow",
        on_internal_error="deny",
    )
    assert validate_firewall_fields(p) is None


def test_validation_rejects_invalid_lifecycle():
    p = _basic_policy(lifecycle="not_a_real_lifecycle")
    err = validate_firewall_fields(p)
    assert err is not None
    assert "lifecycle" in err
    assert "not_a_real_lifecycle" in err


def test_validation_rejects_invalid_mode():
    p = _basic_policy(mode="paranoid")
    err = validate_firewall_fields(p)
    assert err is not None
    assert "mode" in err


def test_validation_rejects_invalid_on_timeout():
    p = _basic_policy(on_timeout="explode")
    err = validate_firewall_fields(p)
    assert err is not None
    assert "on_timeout" in err


# ----- round-trip through policy_store -----


def test_policy_round_trip_preserves_firewall_fields():
    db = _fresh_db()
    try:
        p = _basic_policy(
            name="rt_test",
            lifecycle="before_tool_call",
            mode="shadow",
            priority=50,
            on_timeout="deny",
        )
        create_policy(db, p, actor="test")
        loaded = get_policy(db, "rt_test")
        assert loaded is not None
        assert loaded.lifecycle == "before_tool_call"
        assert loaded.mode == "shadow"
        assert loaded.priority == 50
        assert loaded.on_timeout == "deny"
    finally:
        db.close()


def test_create_policy_rejects_invalid_lifecycle():
    db = _fresh_db()
    try:
        p = _basic_policy(name="bad", lifecycle="evil")
        with pytest.raises(ValueError, match="lifecycle"):
            create_policy(db, p, actor="test")
    finally:
        db.close()


def test_update_policy_can_change_mode():
    db = _fresh_db()
    try:
        p = _basic_policy(name="upd_test", mode="shadow")
        create_policy(db, p, actor="test")
        update_policy(db, "upd_test", mode="enforce", actor="test")
        loaded = get_policy(db, "upd_test")
        assert loaded is not None
        assert loaded.mode == "enforce"
    finally:
        db.close()


def test_update_policy_rejects_invalid_mode():
    db = _fresh_db()
    try:
        p = _basic_policy(name="upd_test")
        create_policy(db, p, actor="test")
        with pytest.raises(ValueError, match="mode"):
            update_policy(db, "upd_test", mode="paranoid", actor="test")
    finally:
        db.close()


def test_update_policy_can_set_circuit_breaker():
    db = _fresh_db()
    try:
        p = _basic_policy(name="cb_test")
        create_policy(db, p, actor="test")
        update_policy(db, "cb_test", circuit_breaker_state="tripped", actor="test")
        loaded = get_policy(db, "cb_test")
        assert loaded is not None
        assert loaded.circuit_breaker_state == "tripped"
    finally:
        db.close()


def test_update_policy_rejects_invalid_circuit_breaker():
    db = _fresh_db()
    try:
        p = _basic_policy(name="cb_test")
        create_policy(db, p, actor="test")
        with pytest.raises(ValueError, match="circuit_breaker_state"):
            update_policy(db, "cb_test", circuit_breaker_state="ohno", actor="test")
    finally:
        db.close()
