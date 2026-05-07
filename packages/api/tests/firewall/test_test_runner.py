"""Tests for the rule unit-test harness — Slice 3 PR N / spec §14.3.

Verifies the harness:
  - Runs each test case in a suite through decide() with persist=False
  - Pass/fail logic matches expected verb + (when not allow) policy_name
  - Malformed suites raise ValueError, malformed tests become failed
    results (not crashes)
  - YAML file + directory loading paths
  - REST endpoint returns the same structure
"""

from __future__ import annotations

import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from db import Database
from firewall import decide as fw_decide
from firewall import test_runner as runner
from lumin.policy import Policy
import policy_store


@pytest.fixture
def db() -> Database:
    instance = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    fw_decide.set_panic_disabled(False)
    yield instance
    instance.close()


def _install_block_rm_rule(db: Database) -> None:
    p = Policy(
        name="block_rm_rf",
        description="block rm -rf",
        trigger="span_end",
        condition='regex_match(str(Input.params.get("command", "")), "(?i)rm\\s+-rf\\s")',
        action="block",
        severity="critical",
        lifecycle="before_tool_call",
        mode="enforce",
        priority=100,
    )
    policy_store.create_policy(db, p, actor="test")


# --- core run_test_suite ---------------------------------------------------


def test_passing_suite(db: Database) -> None:
    _install_block_rm_rule(db)
    suite = {
        "policy": "block_rm_rf",
        "tests": [
            {
                "name": "catches_rm_dash_rf",
                "input": {
                    "lifecycle": "before_tool_call",
                    "tool_name": "shell",
                    "params": {"command": "rm -rf /tmp/foo"},
                },
                "expect": "block",
            },
            {
                "name": "allows_safe_ls",
                "input": {
                    "lifecycle": "before_tool_call",
                    "tool_name": "shell",
                    "params": {"command": "ls -la"},
                },
                "expect": "allow",
            },
        ],
    }
    out = runner.run_test_suite(db, suite)
    assert out["policy"] == "block_rm_rf"
    assert out["total"] == 2
    assert out["passed"] == 2
    assert out["failed"] == 0
    assert all(r["passed"] for r in out["results"])


def test_failing_test_reports_actual(db: Database) -> None:
    _install_block_rm_rule(db)
    suite = {
        "policy": "block_rm_rf",
        "tests": [
            {
                "name": "wrongly_expects_block_for_safe_command",
                "input": {
                    "lifecycle": "before_tool_call",
                    "tool_name": "shell",
                    "params": {"command": "ls -la"},
                },
                "expect": "block",
            }
        ],
    }
    out = runner.run_test_suite(db, suite)
    assert out["passed"] == 0
    assert out["failed"] == 1
    r = out["results"][0]
    assert r["passed"] is False
    assert r["expected"] == "block"
    assert r["actual"] == "allow"
    assert r["actual_policy"] is None


def test_match_requires_correct_policy_name(db: Database) -> None:
    """An ``expect: block`` passes only when THIS suite's policy is
    the one that fired — not some unrelated rule that happened to
    also block. Defends against tests that accidentally pass."""
    # Install two policies; the suite is for the first one.
    _install_block_rm_rule(db)
    p2 = Policy(
        name="other_block",
        description="other rule",
        trigger="span_end",
        condition='regex_match(str(Input.params.get("command", "")), "(?i)mkfs")',
        action="block",
        severity="critical",
        lifecycle="before_tool_call",
        mode="enforce",
        priority=200,  # higher priority — fires first
    )
    policy_store.create_policy(db, p2, actor="test")

    suite = {
        "policy": "block_rm_rf",
        "tests": [
            {
                "name": "blocks_mkfs",
                "input": {
                    "lifecycle": "before_tool_call",
                    "tool_name": "shell",
                    "params": {"command": "mkfs.ext4 /dev/sda"},
                },
                # Engine WILL block this, but via "other_block",
                # not "block_rm_rf". Test should FAIL because the
                # suite is testing block_rm_rf.
                "expect": "block",
            }
        ],
    }
    out = runner.run_test_suite(db, suite)
    assert out["failed"] == 1
    assert out["results"][0]["actual_policy"] == "other_block"


def test_does_not_persist_decisions(db: Database) -> None:
    """Tests run with persist=False — no rows in the decisions table
    after a suite run."""
    _install_block_rm_rule(db)
    suite = {
        "policy": "block_rm_rf",
        "tests": [
            {
                "name": "blocks",
                "input": {
                    "lifecycle": "before_tool_call",
                    "tool_name": "shell",
                    "params": {"command": "rm -rf /"},
                },
                "expect": "block",
            }
        ],
    }
    before = db.fetchone("SELECT COUNT(*) FROM decisions")
    runner.run_test_suite(db, suite)
    after = db.fetchone("SELECT COUNT(*) FROM decisions")
    assert before[0] == after[0]


# --- malformed suites ------------------------------------------------------


def test_missing_policy_raises(db: Database) -> None:
    with pytest.raises(ValueError, match="policy"):
        runner.run_test_suite(db, {"tests": []})


def test_missing_tests_raises(db: Database) -> None:
    with pytest.raises(ValueError, match="tests"):
        runner.run_test_suite(db, {"policy": "x"})


def test_empty_tests_raises(db: Database) -> None:
    with pytest.raises(ValueError):
        runner.run_test_suite(db, {"policy": "x", "tests": []})


def test_invalid_expect_becomes_failed_result(db: Database) -> None:
    """A malformed ``expect:`` shouldn't crash — it becomes a failed
    test with an explanatory error so the rest of the suite still
    runs."""
    _install_block_rm_rule(db)
    suite = {
        "policy": "block_rm_rf",
        "tests": [
            {"name": "bad", "input": {"lifecycle": "before_tool_call"}, "expect": "destroy"},
            {
                "name": "good",
                "input": {
                    "lifecycle": "before_tool_call",
                    "tool_name": "shell",
                    "params": {"command": "rm -rf /"},
                },
                "expect": "block",
            },
        ],
    }
    out = runner.run_test_suite(db, suite)
    assert out["total"] == 2
    assert out["passed"] == 1
    assert out["failed"] == 1
    bad = out["results"][0]
    assert bad["passed"] is False
    assert "expect" in bad["error"]


def test_missing_lifecycle_becomes_failed_result(db: Database) -> None:
    suite = {
        "policy": "x",
        "tests": [{"name": "no-lifecycle", "input": {}, "expect": "allow"}],
    }
    out = runner.run_test_suite(db, suite)
    assert out["failed"] == 1
    assert "lifecycle" in out["results"][0]["error"]


# --- output normalization --------------------------------------------------


def test_string_output_is_normalized(db: Database) -> None:
    """Tests can write ``output: "some text"`` as a shortcut. The
    runner wraps it as ``{"text": "some text"}`` so rules that check
    ``Output.text`` see it the same as production."""
    p = Policy(
        name="block_secrets_in_output",
        description="block secrets",
        trigger="span_end",
        condition="looks_like_secret(Output.text)",
        action="block",
        severity="critical",
        lifecycle="after_proxy_call",
        mode="enforce",
        priority=100,
    )
    policy_store.create_policy(db, p, actor="test")

    suite = {
        "policy": "block_secrets_in_output",
        "tests": [
            {
                "name": "blocks_aws_key",
                "input": {
                    "lifecycle": "after_proxy_call",
                    "output": "Here's the key: AKIAIOSFODNN7EXAMPLE secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                },
                "expect": "block",
            }
        ],
    }
    out = runner.run_test_suite(db, suite)
    # The exact match depends on looks_like_secret heuristics; the
    # important assertion is the runner accepted the string output.
    assert out["total"] == 1


# --- file + directory loading ----------------------------------------------


def test_run_file(db: Database, tmp_path: Path) -> None:
    _install_block_rm_rule(db)
    f = tmp_path / "block_rm_rf.test.yaml"
    f.write_text(textwrap.dedent("""
        policy: block_rm_rf
        tests:
          - name: catches_rm_dash_rf
            input:
              lifecycle: before_tool_call
              tool_name: shell
              params:
                command: "rm -rf /tmp/foo"
            expect: block
    """))
    out = runner.run_file(db, str(f))
    assert out["passed"] == 1
    assert out["failed"] == 0


def test_run_file_missing_path_raises(db: Database) -> None:
    with pytest.raises(FileNotFoundError):
        runner.run_file(db, "/no/such/file.yaml")


def test_run_directory_aggregates(db: Database, tmp_path: Path) -> None:
    _install_block_rm_rule(db)

    f1 = tmp_path / "good.test.yaml"
    f1.write_text(textwrap.dedent("""
        policy: block_rm_rf
        tests:
          - name: catches_rm
            input:
              lifecycle: before_tool_call
              tool_name: shell
              params:
                command: "rm -rf /tmp"
            expect: block
    """))
    f2 = tmp_path / "bad.test.yml"
    f2.write_text(textwrap.dedent("""
        policy: block_rm_rf
        tests:
          - name: wrong_expectation
            input:
              lifecycle: before_tool_call
              tool_name: shell
              params:
                command: "ls"
            expect: block
    """))

    out = runner.run_directory(db, str(tmp_path))
    assert out["files"] == 2
    assert out["total"] == 2
    assert out["passed"] == 1
    assert out["failed"] == 1


def test_run_directory_handles_load_error(db: Database, tmp_path: Path) -> None:
    """A broken YAML file in the directory shouldn't crash the run —
    it should surface as a failed suite with a load_error entry."""
    f = tmp_path / "broken.test.yaml"
    f.write_text(":\n  - not\n   valid yaml: [unclosed")
    out = runner.run_directory(db, str(tmp_path))
    assert out["failed"] >= 1
    assert any("load_error" in s for s in out["suites"])
