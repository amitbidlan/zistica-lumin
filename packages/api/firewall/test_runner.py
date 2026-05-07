"""Rule unit-test harness — Slice 3 PR N / spec §14.3.

Operators write per-rule tests in YAML alongside their policies::

    policy: block_rm_rf
    tests:
      - name: catches_rm_dash_rf_root
        input:
          lifecycle: before_tool_call
          tool_name: shell
          params:
            command: "rm -rf /"
        expect: block

      - name: allows_safe_ls
        input:
          lifecycle: before_tool_call
          tool_name: shell
          params:
            command: "ls -la"
        expect: allow

      - name: rewrites_pii_email_in_output
        input:
          lifecycle: after_proxy_call
          output:
            text: "Send to admin@example.com"
        expect: rewrite

The harness loads the YAML, runs each test through ``decide()`` with
``persist=False``, and reports per-test pass/fail with the actual
decision the engine produced.

Two surfaces:

  - **CLI** (``firewall_test_runner.run_file(path)``): used by ops
    teams in CI. Returns 0 on all-pass, 1 on any failure.
  - **REST endpoint** (``POST /v1/firewall/test/cases``): used by the
    dashboard's "Run tests" button on the policy editor page. Body
    is the same YAML structure parsed into a dict.

Why YAML, not Python tests? The audience is ops + security engineers
who write firewall rules; we don't want to require pytest expertise
to add a regression test. The YAML mirrors the natural language
("when shell command is rm -rf, expect block") that motivates the
rule in the first place.

Tests are RUN against the current engine — same as replay. Operators
who want to test a candidate rule that isn't yet in the DB can pass
``inline_policy`` in the request, which gets registered in a
temporary in-process scope just for that run (lands in a follow-up
when we plumb the plumbing — for now the harness assumes the policy
is already in the DB).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from firewall import decide as fw_decide

logger = logging.getLogger("lumin.api.firewall.test_runner")


# Verbs that are valid in the ``expect:`` field. Maps 1:1 with the
# decide() response decision verb so a test author can write
# whatever the rule's action is and have it match.
VALID_EXPECTATIONS = frozenset({
    "allow", "block", "flag", "require_approval", "rewrite",
})


# ---- public API -----------------------------------------------------------


def run_test_suite(db, suite: Dict[str, Any]) -> Dict[str, Any]:
    """Run a suite — the parsed YAML body, or equivalent dict. Returns
    a structured report::

        {
          "policy": "block_rm_rf",
          "total": 3,
          "passed": 2,
          "failed": 1,
          "results": [
            {"name": "catches_rm_dash_rf_root", "passed": true, ...},
            {"name": "allows_safe_ls", "passed": true, ...},
            {"name": "rewrites_pii", "passed": false,
             "expected": "rewrite", "actual": "allow",
             "actual_policy": null, ...}
          ],
        }

    Raises ``ValueError`` on malformed suite (no ``policy`` field, no
    ``tests``, missing ``expect`` on a test). The runner refuses to
    silently pass a suite it can't parse.
    """
    policy_name = suite.get("policy")
    if not policy_name or not isinstance(policy_name, str):
        raise ValueError("suite must include a top-level 'policy' string")
    tests = suite.get("tests")
    if not tests or not isinstance(tests, list):
        raise ValueError(
            "suite must include a 'tests' list with at least one test case"
        )

    results: List[Dict[str, Any]] = []
    for i, t in enumerate(tests):
        if not isinstance(t, dict):
            raise ValueError(f"test #{i} is not a mapping")
        results.append(_run_one(db, policy_name, i, t))

    passed = sum(1 for r in results if r["passed"])
    return {
        "policy": policy_name,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def run_file(db, path: str) -> Dict[str, Any]:
    """CLI entrypoint. Reads + parses ``path`` (YAML or JSON), runs
    the suite, returns the same structured report ``run_test_suite``
    does. The caller (CLI / CI) translates ``failed > 0`` into a
    nonzero exit code."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"test file not found: {path}")
    with p.open() as f:
        suite = yaml.safe_load(f) or {}
    if not isinstance(suite, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return run_test_suite(db, suite)


def run_directory(db, directory: str) -> Dict[str, Any]:
    """Run every *.yaml / *.yml file in ``directory`` as a suite.
    Aggregates results across files. Returns::

        {
          "files": 3,
          "total": 12,
          "passed": 10,
          "failed": 2,
          "suites": [<per-file run_test_suite output>...]
        }
    """
    p = Path(directory)
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")
    suites: List[Dict[str, Any]] = []
    total = passed = failed = 0
    for f in sorted(list(p.glob("*.yaml")) + list(p.glob("*.yml"))):
        try:
            report = run_file(db, str(f))
        except Exception as e:
            logger.exception("test_runner: failed to load %s", f)
            suites.append({
                "policy": str(f),
                "total": 0, "passed": 0, "failed": 1,
                "load_error": str(e),
                "results": [],
            })
            failed += 1
            total += 1
            continue
        report["file"] = str(f)
        suites.append(report)
        total += report["total"]
        passed += report["passed"]
        failed += report["failed"]
    return {
        "files": len(suites),
        "total": total,
        "passed": passed,
        "failed": failed,
        "suites": suites,
    }


# ---- per-test execution ---------------------------------------------------


def _run_one(
    db, policy_name: str, idx: int, t: Dict[str, Any]
) -> Dict[str, Any]:
    """Run a single test case. Never raises — a malformed test
    becomes a failed result with a descriptive error message,
    not a runtime crash."""
    name = t.get("name") or f"test_{idx}"
    expected = (t.get("expect") or "").lower()
    if expected not in VALID_EXPECTATIONS:
        return {
            "name": name,
            "passed": False,
            "error": f"expect must be one of {sorted(VALID_EXPECTATIONS)}, got {expected!r}",
        }

    inp = t.get("input") or {}
    if not isinstance(inp, dict):
        return {
            "name": name,
            "passed": False,
            "error": "input must be a mapping",
        }
    lifecycle = inp.get("lifecycle")
    if not lifecycle:
        return {
            "name": name,
            "passed": False,
            "error": "input.lifecycle is required",
        }

    output = inp.get("output")
    # Standardize the shape: either a dict or a string in {"text": ...}
    if isinstance(output, str):
        output = {"text": output}

    try:
        resp = fw_decide.decide(
            db,
            lifecycle=lifecycle,
            tool_name=inp.get("tool_name"),
            params=inp.get("params"),
            trace_id=inp.get("trace_id"),
            span_id=inp.get("span_id"),
            session_id=inp.get("session_id"),
            agent=inp.get("agent"),
            project=inp.get("project"),
            model=inp.get("model"),
            messages=inp.get("messages"),
            output=output,
            persist=False,  # test runs never write to the decisions table
        )
    except Exception as e:
        return {
            "name": name,
            "passed": False,
            "error": f"engine crashed: {type(e).__name__}: {e}",
        }

    actual = resp.get("decision", "allow")
    actual_policy = resp.get("policy_name")

    # Match logic: the test passes when the engine's decision verb
    # equals expected AND (when expected != allow) the matched
    # policy is the one this suite is testing. The policy_name check
    # protects against a different rule firing first and "passing"
    # the test by accident.
    if expected == "allow":
        # An expected allow passes if either:
        #   - decision was allow (no rule fired), OR
        #   - decision was allow with this policy explicitly allowing
        ok = actual == "allow"
    else:
        ok = actual == expected and actual_policy == policy_name

    return {
        "name": name,
        "passed": ok,
        "expected": expected,
        "actual": actual,
        "actual_policy": actual_policy,
        "actual_reason": resp.get("reason"),
    }
