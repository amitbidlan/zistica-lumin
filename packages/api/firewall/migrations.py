"""Schema migrations for the Agent Firewall.

Implements §4 of ``docs/AGENT_FIREWALL_SPEC.md``. Idempotent — safe to
call on every startup. Existing ``policies`` table gets four new
columns; new tables are created if absent.

Why a separate module rather than appending to ``db.DUCKDB_SCHEMA``?

  - Keeps the firewall feature self-contained. If we ever ship a
    Lumin build without enforcement (regulated env, OSS-without-
    classifier), this file is the single switch to skip.
  - Idempotent ALTERs read better here than mixed in with the
    initial CREATE TABLE block in ``db.py``.
  - The firewall has more migrations coming (classifier artifact
    table, webhook delivery state, miner runs); centralising lets
    us evolve them without touching ``db.py`` again.

Called from ``db.Database._init_schema`` immediately after the
existing migrations finish.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("lumin.api.firewall.migrations")


# ---- new tables -----------------------------------------------------------
#
# CREATE TABLE IF NOT EXISTS so re-running on an existing DB is a
# no-op. Indexes are CREATE INDEX IF NOT EXISTS for the same reason.
# DuckDB doesn't enforce foreign keys at write time, so we keep the
# referential intent in column comments rather than CONSTRAINT clauses
# (matches how db.py treats spans.trace_id and friends).

_CREATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id VARCHAR PRIMARY KEY,
    policy_id VARCHAR NOT NULL,
    policy_name VARCHAR NOT NULL,
    -- one of: before_proxy_call, after_proxy_call, before_tool_call,
    -- after_tool_call, post_ingest
    lifecycle VARCHAR NOT NULL,
    -- one of: allow, block, flag, require_approval, rewrite
    decision VARCHAR NOT NULL,
    -- mode the policy was in when it fired, captured for back-test
    -- accuracy (a policy promoted to enforce later still shows the
    -- shadow decisions that came before)
    mode_at_decision VARCHAR NOT NULL,
    reason VARCHAR,
    trace_id VARCHAR,
    span_id VARCHAR,
    session_id VARCHAR,
    agent VARCHAR,
    project VARCHAR,
    tool_name VARCHAR,
    -- which field on the input tripped the rule, surfaced as
    -- "Input.command matched pattern" in the decision detail view
    matched_field VARCHAR,
    -- first 200 chars of the offending value, for explainability
    matched_value_truncated VARCHAR,
    decision_at TIMESTAMP NOT NULL,
    duration_ms INTEGER NOT NULL,
    metadata JSON
);
"""

_CREATE_APPROVALS = """
CREATE TABLE IF NOT EXISTS approvals (
    id VARCHAR PRIMARY KEY,
    decision_id VARCHAR NOT NULL,
    policy_id VARCHAR NOT NULL,
    trace_id VARCHAR,
    agent VARCHAR,
    tool_name VARCHAR,
    -- truncated copy of params for operator review without re-fetching
    params_truncated JSON,
    -- one of: pending, allowed, denied, timed_out
    state VARCHAR NOT NULL,
    requested_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP,
    resolved_by VARCHAR,
    resolution_reason VARCHAR,
    -- when the sweeper flips state to timed_out if still pending
    timeout_at TIMESTAMP NOT NULL,
    -- one of: allow, deny — what to do if timeout_at passes
    on_timeout VARCHAR NOT NULL
);
"""

_CREATE_LABELS = """
CREATE TABLE IF NOT EXISTS labels (
    id VARCHAR PRIMARY KEY,
    trace_id VARCHAR,
    span_id VARCHAR,
    -- one of: input, output, tool_params, tool_result
    field VARCHAR NOT NULL,
    -- one of: bad, good, neutral
    label VARCHAR NOT NULL,
    -- free-form, but UI suggests: pii_leak, injection, tool_misuse,
    -- hallucination, other
    category VARCHAR,
    notes VARCHAR,
    labeled_by VARCHAR NOT NULL,
    labeled_at TIMESTAMP NOT NULL
);
"""

_CREATE_PATTERN_SUGGESTIONS = """
CREATE TABLE IF NOT EXISTS pattern_suggestions (
    id VARCHAR PRIMARY KEY,
    -- decision_id or pattern_id from the miner
    source_violation_id VARCHAR,
    -- which template generated this suggestion (see
    -- firewall.suggester.templates.*)
    template VARCHAR NOT NULL,
    draft_yaml VARCHAR NOT NULL,
    suggested_at TIMESTAMP NOT NULL,
    -- non-null once operator clicks "promote to policy"
    promoted_to_policy_id VARCHAR,
    -- non-null if operator dismissed
    dismissed_at TIMESTAMP,
    -- forecast: how many traces this rule would have hit in last 30d
    forecast_fp_count INTEGER,
    -- list of trace_ids representative of forecasted hits
    forecast_fp_examples JSON
);
"""

_CREATE_POLICY_VERSIONS = """
CREATE TABLE IF NOT EXISTS policy_versions (
    id VARCHAR PRIMARY KEY,
    policy_id VARCHAR NOT NULL,
    version_number INTEGER NOT NULL,
    yaml VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    created_by VARCHAR
);
"""

# Single-row-per-key store for firewall feature flags. Today: only
# the panic_disable bit. Keeping it generic so future flags (drift
# threshold, classifier toggles, replay seed) don't each get a
# bespoke table.
_CREATE_FIREWALL_KV = """
CREATE TABLE IF NOT EXISTS firewall_kv (
    k VARCHAR PRIMARY KEY,
    v VARCHAR,
    updated_at TIMESTAMP,
    updated_by VARCHAR
);
"""

# Outbound webhooks (§9.10) and notification channels (§9.11).
# One row per configured destination. Operators add via the dashboard
# or POST /v1/firewall/webhooks; the dispatcher reads on each block-
# class decision.
#
# kind = 'slack' | 'discord' | 'pagerduty' | 'generic' | 'email'
# config_json — kind-specific config (channel, webhook URL, smtp settings,
#               PagerDuty routing key, generic HMAC secret, etc.)
# severity_min — only fire for decisions at or above this severity
# project_filter — only fire for this project (null = all projects)
_CREATE_FIREWALL_WEBHOOKS = """
CREATE TABLE IF NOT EXISTS firewall_webhooks (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    config_json VARCHAR NOT NULL,
    severity_min VARCHAR DEFAULT 'medium',
    project_filter VARCHAR,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    last_fired_at TIMESTAMP,
    last_error VARCHAR
);
"""

# DLQ for webhook deliveries that exhausted retries. The dispatcher
# writes to this on final failure; operators inspect via GET
# /v1/firewall/webhooks/failures.
_CREATE_FIREWALL_WEBHOOK_FAILURES = """
CREATE TABLE IF NOT EXISTS firewall_webhook_failures (
    id VARCHAR PRIMARY KEY,
    webhook_id VARCHAR NOT NULL,
    decision_id VARCHAR,
    attempt_count INTEGER NOT NULL,
    last_error VARCHAR,
    payload_truncated VARCHAR,
    failed_at TIMESTAMP NOT NULL
);
"""


# Indexes — one per query pattern we know we need from §5 of the spec.
# Adding more later is cheap; missing them at launch shows up as a
# slow dashboard.

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_decisions_trace_id ON decisions(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_policy_id ON decisions(policy_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_decision_at ON decisions(decision_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_decision ON decisions(decision)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_session_id ON decisions(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_timeout_at ON approvals(timeout_at)",
    "CREATE INDEX IF NOT EXISTS idx_labels_trace_id ON labels(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_labels_label_category ON labels(label, category)",
    "CREATE INDEX IF NOT EXISTS idx_pattern_suggestions_promoted ON pattern_suggestions(promoted_to_policy_id)",
    "CREATE INDEX IF NOT EXISTS idx_policy_versions_policy_id ON policy_versions(policy_id, version_number DESC)",
)


# ---- existing-table extensions -------------------------------------------
#
# §3 — extend the existing ``policies`` table with the four new fields
# the firewall needs. Defaults are chosen to keep current behavior
# exactly the same: existing rows become ``lifecycle=post_ingest,
# mode=enforce`` which is "evaluate after ingest, take the configured
# action" — i.e. what advisory policies do today.
#
# IMPORTANT: ``mode`` is ``enforce`` for existing rows so the post-
# ingest violation pipeline doesn't silently break on upgrade.
# Operators creating NEW policies via the API/dashboard get
# ``mode=shadow`` per §10.1 (handled at the application layer, not
# the column default — so we can keep this default at ``enforce``
# for back-compat).

_POLICY_EXTENSIONS = (
    "ALTER TABLE policies ADD COLUMN IF NOT EXISTS lifecycle VARCHAR DEFAULT 'post_ingest'",
    "ALTER TABLE policies ADD COLUMN IF NOT EXISTS mode VARCHAR DEFAULT 'enforce'",
    "ALTER TABLE policies ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0",
    "ALTER TABLE policies ADD COLUMN IF NOT EXISTS on_timeout VARCHAR DEFAULT 'allow'",
    # Circuit-breaker state lives on the row so a tripped policy stays
    # tripped across restarts; resetting to 'ok' is an explicit action.
    "ALTER TABLE policies ADD COLUMN IF NOT EXISTS circuit_breaker_state VARCHAR DEFAULT 'ok'",
    # Failure mode for internal errors during evaluation: 'allow' is
    # Rule 7, 'deny' for high-severity rules where false-negative is
    # worse than false-positive.
    "ALTER TABLE policies ADD COLUMN IF NOT EXISTS on_internal_error VARCHAR DEFAULT 'allow'",
)


# ---- entry point ----------------------------------------------------------


def apply(conn) -> None:
    """Apply all firewall schema migrations idempotently.

    Takes a raw DuckDB connection (not the Database wrapper) so it
    can be called from inside ``Database._init_schema`` where the
    lock is already held. Running this twice is harmless — every
    statement is IF NOT EXISTS or column-add-if-absent.

    Failures on individual ALTERs are swallowed (defense in depth,
    matching the existing pattern in ``db.py:151``); migrations must
    not crash startup. We log them so a misconfigured DuckDB version
    or lock contention is at least visible to operators.
    """
    statements: Iterable[str] = (
        _CREATE_DECISIONS,
        _CREATE_APPROVALS,
        _CREATE_LABELS,
        _CREATE_PATTERN_SUGGESTIONS,
        _CREATE_POLICY_VERSIONS,
        _CREATE_FIREWALL_KV,
        _CREATE_FIREWALL_WEBHOOKS,
        _CREATE_FIREWALL_WEBHOOK_FAILURES,
        *_POLICY_EXTENSIONS,
        *_INDEXES,
        # Webhook indexes — dashboard query patterns
        "CREATE INDEX IF NOT EXISTS idx_webhooks_active ON firewall_webhooks(active)",
        "CREATE INDEX IF NOT EXISTS idx_webhook_failures_webhook ON firewall_webhook_failures(webhook_id)",
        "CREATE INDEX IF NOT EXISTS idx_webhook_failures_failed_at ON firewall_webhook_failures(failed_at DESC)",
    )
    for stmt in statements:
        try:
            # Some CREATE TABLE strings contain multiple semicolons via
            # comments; split deliberately on the trailing one only.
            cleaned = stmt.strip()
            if cleaned:
                conn.execute(cleaned)
        except Exception:
            # Defense in depth — never crash startup over a migration
            # hiccup. Log so operators on weird DuckDB versions can
            # see what happened.
            logger.exception("firewall migration failed (continuing): %s", stmt[:120])
