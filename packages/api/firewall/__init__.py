"""Lumin Agent Firewall — real-time policy enforcement on top of the
existing observability stack.

See ``docs/AGENT_FIREWALL_SPEC.md`` for the full specification. This
package implements §2.3's component layout: migrations, decision
engine, decision log, approvals state machine, detectors, suggester,
classifier trainer, miner, and test runner.

The package is intentionally separate from ``policy_runtime.py`` —
that module owns the *post-ingest* (advisory) policy evaluation
which still works exactly as before. The firewall package owns the
new *inline* enforcement at lifecycle hooks ``before_proxy_call``,
``after_proxy_call``, ``before_tool_call``, and ``after_tool_call``.
"""
