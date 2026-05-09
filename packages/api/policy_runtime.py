"""Server-side Policy Engine — extends Accountability Layer Part B
to every Lumin integration regardless of language.

The Python SDK already evaluates policies in-process (fast feedback).
But Mastra, OpenClaw, VoltAgent, and any other framework that ships
spans through `/v1/spans` over HTTP can't take that path — there's
no Python in their address space. Solution: run the engine on the
API side too. Span gets ingested → engine evaluates → violations
land in the same `policy_violations` table the dashboard already
reads from.

Loaded once at API startup from the LUMIN_POLICY_FILE env var. If
the var isn't set, this module is a no-op — zero overhead, no
schema changes, no behavior change.

Per Rule 7, every entry point swallows exceptions. A broken policy
file or evaluation error must never block ingest.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from lumin.policy import (
    PolicyConfigError,
    PolicyEngine,
    PolicyViolation,
    load_policy_engine,
)

import policy_metrics
from db import Database

logger = logging.getLogger("lumin.api.policy")


# --- deterministic violation id --------------------------------------------


def _violation_id(policy_name: str, trace_id: str, span_id: Optional[str]) -> str:
    """Stable id derived from (policy_name, trace_id, span_id).

    Two identical violations (same policy fired against the same span,
    or same trace_end policy fired against the same trace_id) collapse
    onto the same row via the PRIMARY KEY.

    This makes the engine **idempotent** under:
      - OTel retry (same span POSTed twice on transient network)
      - SDK + server both having the engine loaded against the same file
      - re-evaluating the same trace on every span ingest (which we now
        do, so late-arriving children get a chance to flip a trace_end
        policy on)
    """
    raw = f"{policy_name}\x00{trace_id}\x00{span_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# --- SSRF guard for webhook URLs -------------------------------------------


_SSRF_BLOCKED_HOSTS = {
    # AWS instance metadata service
    "169.254.169.254",
    # GCP / Azure metadata service
    "metadata.google.internal",
    "metadata.azure.com",
    # Loopback hostnames (RFC 6761 requires these resolve to 127.0.0.1
    # but we'd rather fail-closed than rely on the resolver). Catches
    # webhook URLs pointed at the same Lumin instance, which would
    # otherwise be a reflection/SSRF amplifier when LUMIN_API_TOKEN
    # is unset.
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
}


def _normalize_host_to_ip(host: str):
    """Return an IP address if `host` is any IPv4/IPv6 representation,
    else None.

    Critical for SSRF defense — attackers use creative IP encodings
    to bypass naive `ipaddress.ip_address(host)` checks:

      127.0.0.1     dotted-quad           ← stdlib parses
      127.1         short-form            ← stdlib REJECTS, inet_aton accepts
      0x7f000001    hex-encoded           ← stdlib REJECTS, inet_aton accepts
      017700000001  octal-encoded         ← stdlib REJECTS, inet_aton accepts
      2130706433    decimal-encoded       ← stdlib REJECTS, inet_aton accepts
      ::1           IPv6 loopback         ← stdlib parses
      [fe80::1]     IPv6 link-local       ← stdlib parses

    Brutal test caught the middle four. The fix: try `socket.inet_aton`
    first (it accepts every POSIX representation) and convert to a
    proper IPv4Address that ip_address.is_private/is_loopback can read.
    """
    import socket

    # IPv4 — accept every legacy POSIX form
    try:
        packed = socket.inet_aton(host)
        return ipaddress.IPv4Address(packed)
    except OSError:
        pass

    # IPv6
    try:
        return ipaddress.IPv6Address(host)
    except (ValueError, ipaddress.AddressValueError):
        pass

    # Standard ip_address as a final pass (covers IPv6 with zones)
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _webhook_url_safe(url: str) -> bool:
    """Reject webhook URLs that point at private/loopback ranges or
    cloud metadata services. Policy YAML is admin-controlled today,
    but a stale/compromised file shouldn't be able to exfiltrate
    AWS instance credentials when an "alert" fires.

    Returns False to block, True to allow. Logs the rejection so an
    operator can spot it.
    """
    if not url:
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    if host in _SSRF_BLOCKED_HOSTS:
        logger.warning("policy: webhook URL blocked (metadata host): %s", url)
        return False

    ip = _normalize_host_to_ip(host)
    if ip is not None:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            logger.warning("policy: webhook URL blocked (private/reserved IP %s): %s", ip, url)
            return False
        # Block the AWS metadata IP even when expressed as int/short-form/etc.
        if str(ip) == "169.254.169.254":
            logger.warning("policy: webhook URL blocked (AWS metadata IP): %s", url)
            return False
    # Else: hostname (DNS name) — allowed; operators are expected to
    # vet the YAML. We don't do DNS resolution here because resolving
    # is itself susceptible to DNS-rebinding attacks at request time.
    return True


_engine_lock = threading.Lock()
_engine: Optional[PolicyEngine] = None
_engine_loaded = False
_engine_path: Optional[str] = None
_engine_mtime: float = 0.0
# Source of the in-memory engine: "yaml" (LUMIN_POLICY_FILE), "db"
# (Phase 4 policies table), or "none" (disabled). Exposed via
# ``engine_source()`` so the dashboard can show a banner without
# inferring it from file mtime.
_engine_source: str = "yaml"
# Cached state token from the DB (row_count, max_version) — bumped
# whenever a CRUD endpoint writes. The engine reload watcher compares
# the live token to this value and rebuilds when they differ.
_engine_db_token: tuple = (0, 0)


def engine_source() -> str:
    """Return where the current in-memory engine was loaded from.

    Values: ``"yaml"`` (LUMIN_POLICY_FILE), ``"db"`` (Phase 4 — policies
    table), ``"none"`` (engine disabled). Used by the dashboard's
    Policies page to render a banner.
    """
    if not _engine_loaded or _engine is None:
        return "none"
    return _engine_source


def _load_into_globals(path: str) -> Optional[PolicyEngine]:
    """Try to load the YAML at `path`. Returns the new engine on
    success, None on no-op (empty path) — re-raises PolicyConfigError
    on invalid YAML so the caller can decide whether to keep the
    previous engine in place.
    """
    if not path:
        return None
    eng = load_policy_engine(path)
    if eng is not None:
        logger.info(
            "policy: loaded %d policies from %s", len(eng.policies), path
        )
    return eng


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def get_engine() -> Optional[PolicyEngine]:
    """Return the process-wide PolicyEngine, lazily loaded on first call.

    Resolution order (Phase 4):
      1. DB — if the ``policies`` table has any enabled rows, build
         the engine from them. This wins over YAML once it's
         populated; bootstrap is the only mechanism that puts rows
         there from a YAML file.
      2. YAML — falls back to ``LUMIN_POLICY_FILE`` when the DB has
         no enabled rows but the file is configured.
      3. None — neither path available; engine disabled.

    First-call errors are logged once. Subsequent reloads happen via
    ``reload_engine()`` (DB-token watcher, mtime watcher, or explicit
    POST).
    """
    global _engine, _engine_loaded, _engine_path, _engine_mtime
    global _engine_source, _engine_db_token
    if _engine_loaded:
        return _engine
    with _engine_lock:
        if _engine_loaded:
            return _engine
        path = os.environ.get("LUMIN_POLICY_FILE")
        _engine_path = path
        try:
            db_engine, db_token = _try_build_db_engine()
            if db_engine is not None:
                _engine = db_engine
                _engine_source = "db"
                _engine_db_token = db_token
                policy_metrics.set_engine_state(
                    loaded=True,
                    policies_count=len(_engine.policies),
                    path="db",
                )
                _engine_loaded = True
                return _engine
        except Exception:
            # DB lookup blew up — log and fall through to YAML so the
            # engine can still come up. A broken DuckDB connection
            # mustn't disable enforcement entirely if a YAML fallback
            # is configured.
            logger.exception(
                "policy: DB engine load failed — falling back to YAML"
            )

        if not path:
            _engine_loaded = True
            policy_metrics.set_engine_state(False)
            return None
        try:
            _engine = _load_into_globals(path)
            _engine_mtime = _file_mtime(path)
            _engine_source = "yaml"
            policy_metrics.set_engine_state(
                loaded=_engine is not None,
                policies_count=len(_engine.policies) if _engine is not None else 0,
                path=path,
                mtime=_engine_mtime,
            )
        except PolicyConfigError as e:
            logger.warning("policy: invalid policy file %s — engine disabled: %s", path, e)
            policy_metrics.record_error("config")
            policy_metrics.set_engine_state(False, path=path)
        except Exception:
            logger.exception("policy: unexpected error loading %s — engine disabled", path)
            policy_metrics.record_error("config")
            policy_metrics.set_engine_state(False, path=path)
        _engine_loaded = True
        return _engine


def _try_build_db_engine(db: Optional["Database"] = None) -> tuple:
    """Read DB → engine, returning (engine, state_token).

    Imported lazily so a clean test environment without the DB module
    available still works. Returns (None, (0, 0)) when the DB has no
    enabled policies — caller will fall back to YAML.

    `db` may be passed in by CRUD handlers so the engine reload uses
    the same connection that just wrote (FastAPI dependency overrides
    swap the DB in tests; bare ``get_db()`` would hit the prod DB).
    """
    try:
        import policy_store
    except ImportError:
        return (None, (0, 0))
    if db is None:
        try:
            from db import get_db
            db = get_db()
        except Exception:
            return (None, (0, 0))
    token = policy_store.policies_state_token(db)
    if token == (0, 0):
        return (None, token)
    eng = policy_store.build_engine_from_db(db)
    return (eng, token)


def reload_engine(db: Optional["Database"] = None) -> dict:
    """Re-read the source of truth and atomically swap the engine.

    Resolution mirrors ``get_engine``:
      1. Try DB first. If the ``policies`` table has enabled rows,
         build the engine from them; this is the Phase 4 happy path.
      2. Fall through to YAML if the DB is empty.

    `db` may be passed by CRUD handlers so the post-write reload sees
    the same connection that just wrote — important under FastAPI
    dependency overrides (tests).

    Behavior on errors:
      - Bad source (YAML typo, DB transient blip) → OLD ENGINE STAYS.
        Production-safety choice over fail-open: a typo during a
        hot-reload must never disable enforcement. Caller sees
        ``{"ok": false, "error": "..."}`` and can re-fix.
      - No source configured → ``{"ok": false, "error": "..."}``.
    """
    global _engine, _engine_loaded, _engine_path, _engine_mtime
    global _engine_source, _engine_db_token
    with _engine_lock:
        # Try DB first
        try:
            db_engine, token = _try_build_db_engine(db=db)
        except Exception as e:
            logger.exception("policy: DB reload crashed")
            policy_metrics.record_error("config")
            db_engine, token = None, _engine_db_token
            db_error = repr(e)
        else:
            db_error = None

        if db_engine is not None:
            _engine = db_engine
            _engine_loaded = True
            _engine_source = "db"
            _engine_db_token = token
            _engine_path = "db"
            policy_metrics.set_engine_state(
                loaded=True,
                policies_count=len(_engine.policies),
                path="db",
            )
            return {
                "ok": True,
                "source": "db",
                "policies": len(_engine.policies),
                "token": list(token),
            }

        # Fall back to YAML
        path = os.environ.get("LUMIN_POLICY_FILE")
        if not path:
            if db_error:
                return {"ok": False, "error": f"db load failed: {db_error}"}
            return {"ok": False, "error": "no source: LUMIN_POLICY_FILE unset and policies table empty"}
        try:
            new_engine = _load_into_globals(path)
        except PolicyConfigError as e:
            logger.warning("policy: hot-reload rejected — bad YAML at %s: %s", path, e)
            policy_metrics.record_error("config")
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception("policy: hot-reload crashed for %s", path)
            policy_metrics.record_error("config")
            return {"ok": False, "error": f"unexpected: {e!r}"}
        _engine = new_engine
        _engine_loaded = True
        _engine_source = "yaml"
        _engine_path = path
        _engine_mtime = _file_mtime(path)
        policy_metrics.set_engine_state(
            loaded=_engine is not None,
            policies_count=len(_engine.policies) if _engine is not None else 0,
            path=path,
            mtime=_engine_mtime,
        )
        return {
            "ok": True,
            "source": "yaml",
            "policies": len(_engine.policies) if _engine is not None else 0,
            "path": path,
            "mtime": _engine_mtime,
        }


def maybe_reload_on_db_token_change() -> bool:
    """DB-token watcher — analog of the YAML mtime watcher.

    Compares the live (row_count, max_version) against the cached
    token. Reloads when they differ, returns True if a reload happened.
    Cheap query (one COUNT + MAX) so safe to call on a fast cadence.
    """
    global _engine_db_token
    try:
        import policy_store
        from db import get_db
    except ImportError:
        return False
    try:
        db = get_db()
        token = policy_store.policies_state_token(db)
    except Exception:
        return False
    if token == _engine_db_token:
        return False
    logger.info(
        "policy: DB token changed (%s → %s); reloading", _engine_db_token, token
    )
    out = reload_engine()
    return bool(out.get("ok"))


def maybe_reload_on_mtime_change() -> bool:
    """Cheap check called by the mtime-watcher background loop.

    Returns True if the engine was actually reloaded. Skip when the
    file is missing or unchanged — no log spam, no work."""
    path = _engine_path or os.environ.get("LUMIN_POLICY_FILE")
    if not path:
        return False
    cur = _file_mtime(path)
    if cur == 0.0 or cur == _engine_mtime:
        return False
    logger.info("policy: file mtime changed (%s → %s); reloading", _engine_mtime, cur)
    out = reload_engine()
    return bool(out.get("ok"))


def _reset_for_tests() -> None:
    """Test helper — wipe cached engine so the next get_engine() reloads."""
    global _engine, _engine_loaded, _engine_path, _engine_mtime
    global _engine_source, _engine_db_token
    with _engine_lock:
        _engine = None
        _engine_loaded = False
        _engine_path = None
        _engine_mtime = 0.0
        _engine_source = "yaml"
        _engine_db_token = (0, 0)
    policy_metrics.reset()


# --- helpers ---------------------------------------------------------------


def _insert_violation(db: Database, v: PolicyViolation) -> None:
    """Direct DB insert — bypasses the /v1/violations HTTP endpoint
    so we don't loop through ourselves. Same table the dashboard
    reads from.

    Idempotent: row id is derived from (policy_name, trace_id, span_id)
    so re-evaluating the same condition produces the same row. The
    PRIMARY KEY on `id` makes ON CONFLICT DO NOTHING a no-op for
    repeat fires.
    """
    # SSRF guard — drop the URL from the stored row if it points at a
    # blocked range, so neither this code path nor a future webhook
    # firer ends up POSTing to it.
    safe_webhook = v.webhook_url if (not v.webhook_url or _webhook_url_safe(v.webhook_url)) else None

    vid = _violation_id(v.policy_name, v.trace_id, v.span_id)
    try:
        db.execute(
            """
            INSERT INTO policy_violations (
                id, policy_name, policy_description, span_id, trace_id,
                condition_text, action_taken, severity, actual_value,
                webhook_fired, webhook_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                vid,
                v.policy_name,
                v.policy_description,
                v.span_id,
                v.trace_id,
                v.condition_text,
                v.action_taken,
                v.severity,
                v.actual_value,
                False,  # webhooks fire from the SDK side; server-side
                        # eval doesn't fire them (we'd need network +
                        # async machinery here, which the prompt put
                        # in the SDK by design).
                safe_webhook,
            ],
        )
    except Exception:
        logger.exception("policy: could not insert violation")


def _resolve_agent_name(db: Database, trace_id: Optional[str]) -> Optional[str]:
    """Look up the trace's name (= agent identity in Phase 1).

    Returns None when the trace doesn't exist yet OR has no name set
    (orphan child span landed before its root populated trace.name).
    The engine treats None as "skip scoped policies", which is the
    safe default — better miss a scoped check than fire it against
    an unknown agent.

    Cheap PRIMARY-KEY lookup, but called per-span so it stays in the
    eval-latency budget. If this becomes a hotspot we can cache by
    trace_id with a short TTL.
    """
    if not trace_id:
        return None
    try:
        row = db.fetchone("SELECT name FROM traces WHERE id = ?", [trace_id])
        if row is None:
            return None
        name = row[0]
        if not name:
            return None
        return str(name)
    except Exception:
        return None


def evaluate_span(db: Database, span_input: Any, trace_aggregates: Optional[Dict[str, Any]] = None) -> int:
    """Run span_end policies against an ingested span.

    `span_input` is the SpanInput pydantic object (or any object with the
    expected fields — model, type, name, duration, etc.). Returns the
    number of violations recorded. All errors are swallowed.
    """
    engine = get_engine()
    if engine is None:
        return 0
    t0 = time.perf_counter()
    n_violations = 0
    try:
        # The PolicyEngine accepts both dicts and objects exposing the
        # right attributes. Build a dict from the SpanInput so we
        # control exactly what the engine sees + can include the
        # API-computed duration.
        span_dict = _span_input_to_dict(span_input)
        trace_id = span_dict.get("trace_id")
        agent_name = _resolve_agent_name(db, trace_id)
        violations = engine.evaluate_span(span_dict, agent_name=agent_name)
        for v in violations:
            _insert_violation(db, v)
            policy_metrics.record_violation(v.policy_name, v.severity)
        n_violations = len(violations)
    except Exception:
        logger.exception("policy: span eval crashed")
        policy_metrics.record_error("eval")
    finally:
        policy_metrics.record_eval(
            "span_end", (time.perf_counter() - t0) * 1000, n_violations
        )
    return n_violations


def evaluate_trace(db: Database, trace_id: str) -> int:
    """Run trace_end policies against a trace by aggregating its
    spans from the DB.

    Called from the spans router when a root span lands. We compute
    span_count, error_count, total_cost, total_tokens directly from
    the spans table — no reliance on stored trace.total_cost_usd,
    which is the same fix sessions.py uses.
    """
    engine = get_engine()
    if engine is None:
        return 0
    t0 = time.perf_counter()
    n_violations = 0
    try:
        # Aggregate from spans + the trace row itself
        agg_row = db.fetchone_dict(
            """
            SELECT
                COUNT(*) AS span_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
                COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
                COALESCE(SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0)), 0) AS total_tokens,
                MIN(started_at) AS first_started_at,
                MAX(ended_at) AS last_ended_at
            FROM spans
            WHERE trace_id = ?
            """,
            [trace_id],
        ) or {}
        trace_row = db.fetchone_dict(
            "SELECT id, name, input, output, session_id, user_id FROM traces WHERE id = ?",
            [trace_id],
        ) or {}

        first = agg_row.get("first_started_at")
        last = agg_row.get("last_ended_at")
        duration_ms = None
        if first and last:
            try:
                duration_ms = int((last - first).total_seconds() * 1000)
            except Exception:
                duration_ms = None

        trace_dict = {
            "id": trace_id,
            "trace_id": trace_id,
            "name": trace_row.get("name"),
            "input": trace_row.get("input"),
            "output": trace_row.get("output"),
            "total_cost_usd": float(agg_row.get("total_cost_usd") or 0.0),
            "total_tokens": int(agg_row.get("total_tokens") or 0),
            "span_count": int(agg_row.get("span_count") or 0),
            "error_count": int(agg_row.get("error_count") or 0),
            "duration_ms": duration_ms,
            "session_id": trace_row.get("session_id"),
            "user_id": trace_row.get("user_id"),
        }
        agent_name = trace_row.get("name") or None
        violations = engine.evaluate_trace(trace_dict, agent_name=agent_name)
        for v in violations:
            _insert_violation(db, v)
            policy_metrics.record_violation(v.policy_name, v.severity)
        n_violations = len(violations)
    except Exception:
        logger.exception("policy: trace eval crashed")
        policy_metrics.record_error("eval")
    finally:
        policy_metrics.record_eval(
            "trace_end", (time.perf_counter() - t0) * 1000, n_violations
        )
    return n_violations


def _span_input_to_dict(span: Any) -> Dict[str, Any]:
    """Project a SpanInput (or dict) into the field set the engine
    reads. Mirrors lumin.policy._span_namespace's expectations.
    """
    if isinstance(span, dict):
        get = span.get
    else:
        def get(k, default=None):
            return getattr(span, k, default)

    return {
        "id": get("id"),
        "trace_id": get("trace_id"),
        "parent_span_id": get("parent_span_id"),
        "name": get("name"),
        "type": get("type"),
        "input": get("input"),
        "output": get("output"),
        "model": get("model"),
        "provider": get("provider"),
        "tokens_input": get("tokens_input"),
        "tokens_output": get("tokens_output"),
        "cost_usd": get("cost_usd"),
        "tool_name": get("tool_name"),
        "session_id": get("session_id"),
        "started_at": get("started_at"),
        "ended_at": get("ended_at"),
        # Translate the SDK's `error` convenience field plus the API's
        # status/error_message pair into a unified status the engine
        # can read via the namespace.
        "error": get("error") or get("error_message"),
        "status": get("status") or ("error" if (get("error") or get("error_message")) else "ok"),
        "span_subtype": get("span_subtype"),
        "thinking_tokens": get("thinking_tokens"),
    }
