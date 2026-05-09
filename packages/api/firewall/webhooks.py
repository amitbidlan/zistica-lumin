"""Webhook outbound + notification channels (§9.10, §9.11).

Owns the dispatcher that fires alerts when a block-class decision
lands. Five destination kinds are supported:

  - slack     — Slack chat.postMessage shape (or webhook URL)
  - discord   — Discord webhook shape
  - pagerduty — PagerDuty Events v2 (trigger event)
  - generic   — POST + HMAC signature
  - email     — SMTP delivery (server config in env)

Behavior contract:

  - Fire-and-forget from the firewall hot path. The dispatcher uses
    a shared executor so a slow webhook never delays a decide()
    response (Rule 7 generalised — agents must not wait on us).
  - Exponential backoff: attempts 1/2/3 separated by 1s/3s/9s.
  - On final failure, write to ``firewall_webhook_failures`` so the
    dashboard surfaces a DLQ.
  - Severity filter — webhooks have a ``severity_min`` so a noisy
    rule doesn't page on-call.

Per spec §10 we never let webhook activity break the firewall:
every dispatch is wrapped in try/except, every error is logged,
and the firewall response returns regardless.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import smtplib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from db import Database

logger = logging.getLogger("lumin.api.firewall.webhooks")


# ---- valid kinds + severity ordering --------------------------------------

VALID_KINDS = frozenset({"slack", "discord", "pagerduty", "generic", "email"})

# Severities in ascending priority. A webhook with severity_min='medium'
# fires for medium / high / critical decisions, not low.
_SEVERITY_RANK: Dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


def _severity_passes(decision_severity: Optional[str], min_severity: str) -> bool:
    decision_rank = _SEVERITY_RANK.get((decision_severity or "medium").lower(), 1)
    min_rank = _SEVERITY_RANK.get((min_severity or "medium").lower(), 1)
    return decision_rank >= min_rank


# ---- shared dispatcher executor -------------------------------------------
#
# 4 worker threads is plenty: webhooks are I/O-bound and we don't
# expect more than a handful of subscribers per deployment. Bigger
# pools would just add memory overhead. The pool is process-global
# and lazy-initialised — tests that need to await all dispatches
# call ``shutdown_for_tests()``.

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: Optional[ThreadPoolExecutor] = None


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="lumin-webhooks"
            )
        return _EXECUTOR


def shutdown_for_tests() -> None:
    """Test helper. Drains pending webhook dispatches so test
    assertions don't race with background threads."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=True)
            _EXECUTOR = None


# ---- public types ---------------------------------------------------------


@dataclass
class WebhookConfig:
    id: str
    name: str
    kind: str
    config: Dict[str, Any]
    severity_min: str
    project_filter: Optional[str]
    active: bool


# ---- config CRUD ----------------------------------------------------------


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_webhook(
    db: Database,
    *,
    name: str,
    kind: str,
    config: Dict[str, Any],
    severity_min: str = "medium",
    project_filter: Optional[str] = None,
) -> WebhookConfig:
    """Insert a new webhook config row."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown webhook kind: {kind!r}")
    if severity_min not in _SEVERITY_RANK:
        raise ValueError(f"severity_min must be one of {sorted(_SEVERITY_RANK)}")
    _validate_kind_config(kind, config)

    wh_id = "wh_" + uuid.uuid4().hex[:24]
    now = _utc_now_naive()
    db.execute(
        """
        INSERT INTO firewall_webhooks (
            id, name, kind, config_json, severity_min,
            project_filter, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            wh_id, name, kind, json.dumps(config),
            severity_min, project_filter, True, now, now,
        ],
    )
    return WebhookConfig(
        id=wh_id, name=name, kind=kind, config=config,
        severity_min=severity_min, project_filter=project_filter, active=True,
    )


def list_webhooks(db: Database) -> List[Dict[str, Any]]:
    rows = db.fetchall_dict(
        """
        SELECT id, name, kind, config_json, severity_min,
               project_filter, active, created_at, updated_at,
               last_fired_at, last_error
        FROM firewall_webhooks
        ORDER BY created_at DESC
        """
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            cfg = json.loads(r.get("config_json") or "{}")
        except Exception:
            cfg = {}
        # Don't echo secrets back over the API. Mask config values
        # whose key suggests sensitivity.
        cfg = _mask_secret_fields(cfg)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "config": cfg,
            "severity_min": r.get("severity_min") or "medium",
            "project_filter": r.get("project_filter"),
            "active": bool(r.get("active") or False),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "last_fired_at": r.get("last_fired_at"),
            "last_error": r.get("last_error"),
        })
    return out


def delete_webhook(db: Database, webhook_id: str) -> bool:
    row = db.fetchone("SELECT id FROM firewall_webhooks WHERE id = ?", [webhook_id])
    if row is None:
        return False
    db.execute("DELETE FROM firewall_webhooks WHERE id = ?", [webhook_id])
    return True


def _mask_secret_fields(cfg: Dict[str, Any]) -> Dict[str, Any]:
    masked: Dict[str, Any] = {}
    for k, v in cfg.items():
        kl = k.lower()
        if isinstance(v, str) and any(
            tok in kl for tok in ("token", "secret", "key", "password", "auth")
        ):
            masked[k] = _mask_value(v)
        else:
            masked[k] = v
    return masked


def _mask_value(v: str) -> str:
    if len(v) <= 6:
        return "****"
    return v[:3] + "…" + v[-3:]


def _validate_kind_config(kind: str, config: Dict[str, Any]) -> None:
    """Per-kind required fields + URL safety. Errors here surface as
    400 from the create endpoint.

    SSRF guard (Slice 6A.1 / brutal-test fix): every URL-shaped
    config field is validated against ``policy_runtime._webhook_url_safe``
    which rejects:
      - non-http(s) schemes (file://, gopher://, ftp://, etc.)
      - loopback / private / link-local / reserved IPs
      - AWS instance metadata host + IP variants
    Without this check, an operator (or anyone with write access to
    /v1/firewall/webhooks) could exfiltrate cloud creds by pointing
    a webhook at 169.254.169.254 and triggering any block decision.
    """
    required: Dict[str, List[str]] = {
        "slack": ["webhook_url"],
        "discord": ["webhook_url"],
        "pagerduty": ["routing_key"],
        "generic": ["url"],
        "email": ["to"],
    }
    needed = required.get(kind, [])
    missing = [k for k in needed if not config.get(k)]
    if missing:
        raise ValueError(
            f"webhook kind={kind!r} requires config keys: {missing}"
        )

    # URL-safety check — reuses the validator that already protects
    # the legacy policy_violations.webhook_url field.
    from policy_runtime import _webhook_url_safe
    url_keys = ("webhook_url", "url")
    for key in url_keys:
        url = config.get(key)
        if not isinstance(url, str) or not url:
            continue
        if not _webhook_url_safe(url):
            raise ValueError(
                f"webhook url rejected by SSRF guard "
                f"(blocked scheme / private host / cloud metadata): {url!r}"
            )


# ---- dispatch on decision -------------------------------------------------


def fire_for_decision(
    db: Database,
    *,
    decision_id: str,
    decision: str,
    severity: Optional[str],
    policy_name: Optional[str],
    project: Optional[str],
    reason: Optional[str],
    trace_id: Optional[str],
    mode_at_decision: Optional[str],
) -> int:
    """Dispatch the relevant webhooks for a decision. Returns the
    number of webhook deliveries scheduled (not the count completed —
    delivery happens in the executor).

    Called from ``firewall.decide._record_decision`` after a block-
    class verb fires. Runs synchronously up to the executor.submit
    call; the actual HTTP/SMTP work happens off-thread.

    Per Rule 7, every error is swallowed. The firewall response must
    not depend on webhook firing succeeding.
    """
    try:
        webhooks = _list_active_for_dispatch(db, project=project)
    except Exception:
        logger.exception("webhooks: failed to list webhooks for decision %s", decision_id)
        return 0

    scheduled = 0
    for wh in webhooks:
        if not _severity_passes(severity, wh.severity_min):
            continue
        payload = _build_payload(
            kind=wh.kind,
            decision_id=decision_id,
            decision=decision,
            severity=severity or "medium",
            policy_name=policy_name or "_engine_",
            project=project,
            reason=reason or "",
            trace_id=trace_id,
            mode_at_decision=mode_at_decision or "enforce",
            webhook_name=wh.name,
        )
        # Fire-and-forget. Errors land in the DLQ via _attempt_with_retries.
        try:
            _get_executor().submit(
                _attempt_with_retries,
                webhook=wh,
                payload=payload,
                decision_id=decision_id,
                # Pass the duckdb path explicitly — the worker thread
                # opens its own connection on failure paths because the
                # main Database object isn't safe for cross-thread use.
                duckdb_path=db.duckdb_path,
                sqlite_path=db.sqlite_path,
            )
            scheduled += 1
        except Exception:
            logger.exception("webhooks: failed to schedule dispatch for %s", wh.id)
    return scheduled


def _list_active_for_dispatch(
    db: Database, *, project: Optional[str]
) -> List[WebhookConfig]:
    rows = db.fetchall_dict(
        """
        SELECT id, name, kind, config_json, severity_min, project_filter
        FROM firewall_webhooks
        WHERE active = true
          AND (project_filter IS NULL OR project_filter = ?)
        """,
        [project or ""],
    )
    out: List[WebhookConfig] = []
    for r in rows:
        try:
            cfg = json.loads(r.get("config_json") or "{}")
        except Exception:
            cfg = {}
        out.append(WebhookConfig(
            id=r["id"], name=r["name"], kind=r["kind"], config=cfg,
            severity_min=r.get("severity_min") or "medium",
            project_filter=r.get("project_filter"),
            active=True,
        ))
    return out


# ---- payload builders -----------------------------------------------------


def _build_payload(
    *,
    kind: str,
    decision_id: str,
    decision: str,
    severity: str,
    policy_name: str,
    project: Optional[str],
    reason: str,
    trace_id: Optional[str],
    mode_at_decision: str,
    webhook_name: str,
) -> Dict[str, Any]:
    """Kind-specific payload shape. The dispatcher serialises this
    differently per kind (JSON for HTTP, structured for SMTP)."""
    title = f"Lumin firewall · {decision} · {policy_name}"
    summary = (
        f"Decision {decision_id} ({decision}, mode={mode_at_decision}) "
        f"matched policy {policy_name}"
    )
    if reason:
        summary += f" — {reason[:140]}"

    if kind == "slack":
        return {
            "text": title,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{title}*\n"
                            f"• policy: `{policy_name}`\n"
                            f"• severity: `{severity}`\n"
                            f"• mode: `{mode_at_decision}`\n"
                            f"• project: `{project or 'default'}`\n"
                            f"• trace: `{trace_id or '—'}`\n"
                            f"• reason: {reason[:200] if reason else '—'}"
                        ),
                    },
                }
            ],
        }
    if kind == "discord":
        return {
            "content": title,
            "embeds": [
                {
                    "title": title,
                    "description": summary[:1900],
                    "color": _severity_color(severity),
                    "fields": [
                        {"name": "policy", "value": policy_name, "inline": True},
                        {"name": "severity", "value": severity, "inline": True},
                        {"name": "mode", "value": mode_at_decision, "inline": True},
                        {"name": "project", "value": project or "default", "inline": True},
                        {"name": "trace_id", "value": trace_id or "—", "inline": False},
                    ],
                }
            ],
        }
    if kind == "pagerduty":
        return {
            "event_action": "trigger",
            "dedup_key": f"lumin:{policy_name}:{trace_id or decision_id}",
            "payload": {
                "summary": summary[:1024],
                "source": "lumin-firewall",
                "severity": _pagerduty_severity(severity),
                "component": "agent-firewall",
                "group": project or "default",
                "class": "policy_violation",
                "custom_details": {
                    "decision_id": decision_id,
                    "decision": decision,
                    "policy_name": policy_name,
                    "mode_at_decision": mode_at_decision,
                    "trace_id": trace_id,
                    "project": project,
                    "reason": reason,
                },
            },
        }
    if kind == "email":
        return {
            "subject": title,
            "body": (
                f"{summary}\n\n"
                f"decision_id: {decision_id}\n"
                f"policy:      {policy_name}\n"
                f"severity:    {severity}\n"
                f"mode:        {mode_at_decision}\n"
                f"project:     {project or 'default'}\n"
                f"trace_id:    {trace_id or '—'}\n"
                f"reason:      {reason or '—'}\n"
            ),
            "title": title,
        }
    # generic — full envelope, caller signs with HMAC
    return {
        "type": "lumin.firewall.decision",
        "decision_id": decision_id,
        "decision": decision,
        "policy_name": policy_name,
        "severity": severity,
        "mode_at_decision": mode_at_decision,
        "project": project,
        "trace_id": trace_id,
        "reason": reason,
        "webhook_name": webhook_name,
        "fired_at": datetime.now(timezone.utc).isoformat(),
    }


def _severity_color(severity: str) -> int:
    return {
        "low": 0x95A5A6,        # slate
        "medium": 0xF1C40F,     # amber
        "high": 0xE67E22,       # orange
        "critical": 0xE74C3C,   # rose
    }.get((severity or "medium").lower(), 0xF1C40F)


def _pagerduty_severity(severity: str) -> str:
    """Map our 4-level severity to PagerDuty's vocabulary."""
    return {
        "low": "info",
        "medium": "warning",
        "high": "error",
        "critical": "critical",
    }.get((severity or "medium").lower(), "warning")


# ---- HTTP / SMTP delivery -------------------------------------------------


_BACKOFF_SECONDS = (1, 3, 9)
_HTTP_TIMEOUT_S = 5
_TRUNCATE_PAYLOAD_AT = 8000


def _attempt_with_retries(
    *,
    webhook: WebhookConfig,
    payload: Dict[str, Any],
    decision_id: str,
    duckdb_path: str,
    sqlite_path: str,
) -> None:
    """Try to deliver once, retry up to 2 more times with backoff,
    record DLQ on final failure. Runs in the executor."""
    last_error: Optional[str] = None
    for attempt in range(1, 4):
        try:
            _deliver_once(webhook, payload)
            _mark_fired(duckdb_path, sqlite_path, webhook.id, error=None)
            return
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "webhooks: %s attempt %d/3 failed: %s",
                webhook.id, attempt, last_error,
            )
            if attempt < 3:
                time.sleep(_BACKOFF_SECONDS[attempt - 1])
    # Exhausted retries — record DLQ entry.
    try:
        _record_dlq(
            duckdb_path, sqlite_path,
            webhook_id=webhook.id, decision_id=decision_id,
            attempt_count=3, last_error=last_error or "unknown",
            payload=payload,
        )
        _mark_fired(duckdb_path, sqlite_path, webhook.id, error=last_error or "exhausted")
    except Exception:
        logger.exception("webhooks: failed to write DLQ row for %s", webhook.id)


def _deliver_once(webhook: WebhookConfig, payload: Dict[str, Any]) -> None:
    if webhook.kind == "email":
        _deliver_email(webhook, payload)
        return
    if webhook.kind == "pagerduty":
        _deliver_http_json(
            url="https://events.pagerduty.com/v2/enqueue",
            payload=payload,
            extra_headers={},
            secret=None,
        )
        return
    if webhook.kind == "slack":
        url = webhook.config.get("webhook_url") or ""
        _deliver_http_json(url=url, payload=payload)
        return
    if webhook.kind == "discord":
        url = webhook.config.get("webhook_url") or ""
        _deliver_http_json(url=url, payload=payload)
        return
    if webhook.kind == "generic":
        url = webhook.config.get("url") or ""
        secret = webhook.config.get("hmac_secret")
        _deliver_http_json(url=url, payload=payload, secret=secret)
        return
    raise ValueError(f"unknown webhook kind: {webhook.kind}")


def _deliver_http_json(
    *,
    url: str,
    payload: Dict[str, Any],
    extra_headers: Optional[Dict[str, str]] = None,
    secret: Optional[str] = None,
) -> None:
    if not url:
        raise ValueError("webhook url is empty")
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "lumin-firewall/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    if secret:
        signature = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        headers["X-Lumin-Signature"] = f"sha256={signature}"

    req = urllib_request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            status = resp.getcode()
    except urllib_error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}") from e
    except urllib_error.URLError as e:
        raise RuntimeError(f"URL error: {e.reason}") from e
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")


def _deliver_email(webhook: WebhookConfig, payload: Dict[str, Any]) -> None:
    """SMTP delivery using env-configured server. Operator-driven
    (no SaaS escape hatch) so credentials stay on-box.

    Required env: ``LUMIN_SMTP_HOST``. Optional: PORT, USERNAME,
    PASSWORD, FROM, USE_TLS. Per-webhook ``to`` is in config.
    """
    smtp_host = os.environ.get("LUMIN_SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("LUMIN_SMTP_HOST not configured")

    smtp_port = int(os.environ.get("LUMIN_SMTP_PORT", "587"))
    smtp_user = os.environ.get("LUMIN_SMTP_USERNAME")
    smtp_pass = os.environ.get("LUMIN_SMTP_PASSWORD")
    smtp_from = os.environ.get("LUMIN_SMTP_FROM", "lumin-firewall@localhost")
    use_tls = (
        os.environ.get("LUMIN_SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
    )

    recipients = webhook.config.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]

    msg = EmailMessage()
    msg["Subject"] = payload.get("subject") or "Lumin firewall alert"
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(payload.get("body") or "")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=_HTTP_TIMEOUT_S) as server:
        if use_tls:
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def _mark_fired(
    duckdb_path: str, sqlite_path: str, webhook_id: str, *, error: Optional[str]
) -> None:
    """Update last_fired_at + last_error on the row. Opens its own
    Database connection because the dispatcher runs in a worker
    thread."""
    db = Database(duckdb_path=duckdb_path, sqlite_path=sqlite_path)
    try:
        db.execute(
            """
            UPDATE firewall_webhooks
               SET last_fired_at = ?, last_error = ?, updated_at = ?
             WHERE id = ?
            """,
            [_utc_now_naive(), error, _utc_now_naive(), webhook_id],
        )
    except Exception:
        logger.exception("webhooks: _mark_fired failed for %s", webhook_id)
    finally:
        db.close()


def _record_dlq(
    duckdb_path: str,
    sqlite_path: str,
    *,
    webhook_id: str,
    decision_id: str,
    attempt_count: int,
    last_error: str,
    payload: Dict[str, Any],
) -> None:
    db = Database(duckdb_path=duckdb_path, sqlite_path=sqlite_path)
    try:
        try:
            payload_str = json.dumps(payload, default=str)[:_TRUNCATE_PAYLOAD_AT]
        except Exception:
            payload_str = str(payload)[:_TRUNCATE_PAYLOAD_AT]
        db.execute(
            """
            INSERT INTO firewall_webhook_failures (
                id, webhook_id, decision_id, attempt_count,
                last_error, payload_truncated, failed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "fail_" + uuid.uuid4().hex[:24],
                webhook_id, decision_id, attempt_count,
                last_error[:500], payload_str, _utc_now_naive(),
            ],
        )
    finally:
        db.close()


def list_failures(db: Database, *, limit: int = 100) -> List[Dict[str, Any]]:
    rows = db.fetchall_dict(
        """
        SELECT id, webhook_id, decision_id, attempt_count,
               last_error, failed_at
        FROM firewall_webhook_failures
        ORDER BY failed_at DESC
        LIMIT ?
        """,
        [limit],
    )
    return [
        {
            "id": r["id"],
            "webhook_id": r["webhook_id"],
            "decision_id": r.get("decision_id"),
            "attempt_count": int(r.get("attempt_count") or 0),
            "last_error": r.get("last_error"),
            "failed_at": r.get("failed_at"),
        }
        for r in rows
    ]
