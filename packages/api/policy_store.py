"""DB-backed policy storage — Phase 4 of Accountability Layer Part B.

Phases 1–3 used a YAML file as the source of truth, with a mtime
watcher to hot-reload changes. Phase 4 moves authoritative storage
into DuckDB so policies can be created, edited, and deleted from the
dashboard UI.

The YAML file remains relevant only as a one-shot bootstrap: when the
``policies`` table is empty AND ``LUMIN_POLICY_FILE`` is set, we import
its rules once at startup. After that, the DB wins. Editing the YAML
on disk no longer affects the running engine — the file watcher is
disabled when DB is the source.

Design notes:

* The engine cares only about ``Policy`` dataclasses (defined in the
  SDK). This module produces and consumes them; it doesn't redefine
  the shape.

* Audit log writes happen in the same transaction as the policy
  write — DuckDB's autocommit is fine here because both writes share
  the same connection lock from ``Database``.

* Versions are scoped per-row, but the engine watches ``max(version)``
  + row count for cache invalidation. Bumping any row is enough to
  trigger a reload; the cost of re-reading the table is bounded by
  policy count (typically <50).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from lumin.policy import (
    Policy,
    PolicyConfigError,
    PolicyEngine,
    load_policy_engine,
)

from db import Database

logger = logging.getLogger("lumin.api.policy_store")


# ---- shape helpers --------------------------------------------------------


def _policy_to_row(p: Policy) -> dict:
    return {
        "name": p.name,
        "description": p.description,
        "trigger": p.trigger,
        "condition": p.condition,
        "action": p.action,
        "severity": p.severity,
        "webhook_url": p.webhook_url,
        "scope_agents": json.dumps(list(p.scope_agents or [])),
        "enabled": True,
        # Agent Firewall fields (§3 of AGENT_FIREWALL_SPEC.md). Pass
        # through as-is — the column defaults handle rows without
        # these fields, and validate_policy_for_save catches bad
        # values before we get here.
        "lifecycle": getattr(p, "lifecycle", "post_ingest"),
        "mode": getattr(p, "mode", "enforce"),
        "priority": int(getattr(p, "priority", 0)),
        "on_timeout": getattr(p, "on_timeout", "allow"),
        "on_internal_error": getattr(p, "on_internal_error", "allow"),
        "circuit_breaker_state": getattr(p, "circuit_breaker_state", "ok"),
    }


def _row_to_policy(row: dict) -> Policy:
    """Construct a Policy from a ``policies`` row.

    Tolerates both JSON-encoded scope_agents (DB read) and an already-
    decoded list (test fixtures or manual inserts). Tolerates the
    firewall fields being absent (older DBs from before the
    migration ran) by falling back to the dataclass defaults.
    """
    raw_scope = row.get("scope_agents")
    scope: List[str] = []
    if raw_scope:
        if isinstance(raw_scope, str):
            try:
                decoded = json.loads(raw_scope)
            except json.JSONDecodeError:
                decoded = []
        else:
            decoded = raw_scope
        if isinstance(decoded, list):
            scope = [str(x) for x in decoded if isinstance(x, str)]

    return Policy(
        name=str(row["name"]),
        description=row.get("description"),
        trigger=str(row["trigger"]),
        condition=str(row["condition"]),
        action=str(row["action"]),
        severity=str(row["severity"]),
        webhook_url=row.get("webhook_url"),
        scope_agents=scope,
        lifecycle=str(row.get("lifecycle") or "post_ingest"),
        mode=str(row.get("mode") or "enforce"),
        priority=int(row.get("priority") or 0),
        on_timeout=str(row.get("on_timeout") or "allow"),
        on_internal_error=str(row.get("on_internal_error") or "allow"),
        circuit_breaker_state=str(row.get("circuit_breaker_state") or "ok"),
    )


# ---- firewall field validation -------------------------------------------
#
# Called from create_policy / update_policy paths. Catches bad values
# at the API boundary (HTTP 400) so the engine never has to reason
# about lifecycle="ohno" or mode="???".

VALID_LIFECYCLES = frozenset({
    "post_ingest",
    "before_proxy_call",
    "after_proxy_call",
    "before_tool_call",
    "after_tool_call",
})
VALID_MODES = frozenset({"shadow", "flag", "enforce"})
VALID_ON_TIMEOUT = frozenset({"allow", "deny"})
VALID_ON_INTERNAL_ERROR = frozenset({"allow", "deny", "flag"})


def validate_firewall_fields(p: Policy) -> Optional[str]:
    """Returns ``None`` when the firewall fields on ``p`` are valid,
    else a human-readable error string suitable for an HTTP 400.

    Cheap to call repeatedly — used both at HTTP-handler validation
    time and at engine-load time as a defense-in-depth check.
    """
    lc = getattr(p, "lifecycle", "post_ingest")
    if lc not in VALID_LIFECYCLES:
        return (
            f"lifecycle '{lc}' is not one of {sorted(VALID_LIFECYCLES)}"
        )
    mode = getattr(p, "mode", "enforce")
    if mode not in VALID_MODES:
        return f"mode '{mode}' is not one of {sorted(VALID_MODES)}"
    ot = getattr(p, "on_timeout", "allow")
    if ot not in VALID_ON_TIMEOUT:
        return f"on_timeout '{ot}' is not one of {sorted(VALID_ON_TIMEOUT)}"
    oie = getattr(p, "on_internal_error", "allow")
    if oie not in VALID_ON_INTERNAL_ERROR:
        return (
            f"on_internal_error '{oie}' is not one of "
            f"{sorted(VALID_ON_INTERNAL_ERROR)}"
        )
    pri = getattr(p, "priority", 0)
    try:
        int(pri)
    except (TypeError, ValueError):
        return f"priority must be an integer, got {pri!r}"
    return None


# ---- read paths -----------------------------------------------------------


def list_policies(db: Database, include_disabled: bool = False) -> List[Policy]:
    """All policies in the DB (engine-shape Policy objects).

    By default returns only enabled rows — the engine never sees
    disabled policies, and the read endpoint defaults match. Pass
    ``include_disabled=True`` to surface soft-deleted rows for the
    audit/history UI.
    """
    where = "" if include_disabled else "WHERE enabled = true"
    rows = db.fetchall_dict(
        f"SELECT * FROM policies {where} ORDER BY name"
    )
    return [_row_to_policy(r) for r in rows]


def get_policy(db: Database, name: str) -> Optional[Policy]:
    row = db.fetchone_dict(
        "SELECT * FROM policies WHERE name = ? AND enabled = true",
        [name],
    )
    return _row_to_policy(row) if row else None


def has_any_policies(db: Database) -> bool:
    """Cheap existence check — used to decide whether YAML bootstrap
    should run. Counts rows regardless of enabled flag, so bootstrapping
    doesn't re-import after every soft-delete."""
    row = db.fetchone("SELECT COUNT(*) FROM policies")
    return bool(row and (row[0] or 0) > 0)


def policies_state_token(db: Database) -> Tuple[int, int]:
    """Compact state token used by the engine's reload check.

    Returns ``(row_count, max_version)``. Either changing implies the
    engine's cache is stale. Cheaper than re-reading every row on
    every span — the runtime polls this every N seconds.

    Counts ONLY ``lifecycle = 'post_ingest'`` rows because that's
    what the SDK PolicyEngine evaluates. Firewall rules are picked
    up by the synchronous decide engine via its own per-request
    SELECT and don't go through this token. Without this filter, an
    install with only firewall rules would: (a) report a non-zero
    token, (b) trigger reload_engine to build a DB-backed engine,
    (c) get None back from build_engine_from_db (since it filters
    too), (d) clear the in-memory engine, (e) on the next span fall
    through to YAML — which silently disables YAML-supplied rules
    that *should* still fire. Lockstep filtering keeps the watcher
    inert when only firewall rules exist.
    """
    row = db.fetchone(
        """
        SELECT COUNT(*), COALESCE(MAX(version), 0) FROM policies
        WHERE enabled = true
          AND COALESCE(lifecycle, 'post_ingest') = 'post_ingest'
        """
    )
    if not row:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


# ---- write paths ----------------------------------------------------------


def create_policy(
    db: Database, p: Policy, actor: Optional[str] = None
) -> Policy:
    """Insert a new policy. Validates shape via ``Policy`` itself; the
    caller is responsible for catching ``PolicyConfigError`` from
    condition-AST validation.

    Raises ``ValueError`` if the name already exists (caller maps to
    HTTP 409 Conflict).
    """
    # Validate firewall fields up-front so a 400 surfaces before we
    # touch the DB. Cheap; defense in depth on top of the engine's
    # own validation.
    err = validate_firewall_fields(p)
    if err:
        raise ValueError(f"policy '{p.name}': {err}")

    existing = db.fetchone_dict(
        "SELECT name, enabled FROM policies WHERE name = ?", [p.name]
    )
    if existing:
        # If the row was soft-deleted, allow re-creation by re-enabling
        # the existing record + bumping version. This avoids leaving
        # orphan disabled rows behind every time someone "deletes then
        # recreates" through the UI, while still preserving the audit
        # row from the original delete.
        if existing.get("enabled"):
            raise ValueError(f"policy '{p.name}' already exists")
        return update_policy(
            db,
            p.name,
            description=p.description,
            trigger=p.trigger,
            condition=p.condition,
            action=p.action,
            severity=p.severity,
            webhook_url=p.webhook_url,
            scope_agents=list(p.scope_agents or []),
            enabled=True,
            lifecycle=getattr(p, "lifecycle", "post_ingest"),
            mode=getattr(p, "mode", "enforce"),
            priority=int(getattr(p, "priority", 0)),
            on_timeout=getattr(p, "on_timeout", "allow"),
            on_internal_error=getattr(p, "on_internal_error", "allow"),
            actor=actor,
            audit_action="create",
        )

    row = _policy_to_row(p)
    db.execute(
        """
        INSERT INTO policies (
            name, description, trigger, condition, action, severity,
            webhook_url, scope_agents, enabled, version,
            lifecycle, mode, priority, on_timeout, on_internal_error,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row["name"], row["description"], row["trigger"], row["condition"],
            row["action"], row["severity"], row["webhook_url"],
            row["scope_agents"], row["enabled"],
            row["lifecycle"], row["mode"], row["priority"],
            row["on_timeout"], row["on_internal_error"],
            datetime.now(timezone.utc).replace(tzinfo=None),
            datetime.now(timezone.utc).replace(tzinfo=None),
        ],
    )
    _audit(db, p.name, "create", before=None, after=row, actor=actor)
    _snapshot_version(db, p.name, actor=actor)
    saved = get_policy(db, p.name)
    assert saved is not None  # we just inserted it
    return saved


def update_policy(
    db: Database,
    name: str,
    *,
    description: Optional[str] = None,
    trigger: Optional[str] = None,
    condition: Optional[str] = None,
    action: Optional[str] = None,
    severity: Optional[str] = None,
    webhook_url: Optional[str] = None,
    scope_agents: Optional[List[str]] = None,
    enabled: Optional[bool] = None,
    # Agent Firewall fields (§3 of AGENT_FIREWALL_SPEC.md). All
    # optional — pass None to leave the existing value untouched.
    lifecycle: Optional[str] = None,
    mode: Optional[str] = None,
    priority: Optional[int] = None,
    on_timeout: Optional[str] = None,
    on_internal_error: Optional[str] = None,
    circuit_breaker_state: Optional[str] = None,
    actor: Optional[str] = None,
    audit_action: str = "update",
) -> Policy:
    """Partial update — only non-None fields overwrite. Bumps version,
    writes an audit row, returns the merged Policy.

    Raises ``KeyError`` if the policy doesn't exist (caller maps to
    HTTP 404).
    """
    before = db.fetchone_dict("SELECT * FROM policies WHERE name = ?", [name])
    if before is None:
        raise KeyError(name)

    fields: List[str] = []
    params: list = []
    if description is not None:
        fields.append("description = ?"); params.append(description)
    if trigger is not None:
        fields.append("trigger = ?"); params.append(trigger)
    if condition is not None:
        fields.append("condition = ?"); params.append(condition)
    if action is not None:
        fields.append("action = ?"); params.append(action)
    if severity is not None:
        fields.append("severity = ?"); params.append(severity)
    if webhook_url is not None:
        fields.append("webhook_url = ?"); params.append(webhook_url)
    if scope_agents is not None:
        fields.append("scope_agents = ?"); params.append(json.dumps(list(scope_agents)))
    if enabled is not None:
        fields.append("enabled = ?"); params.append(bool(enabled))
    # Agent Firewall fields. Validated up-front so a bad mode/lifecycle
    # surfaces as a 400 rather than landing in the DB.
    if lifecycle is not None:
        if lifecycle not in VALID_LIFECYCLES:
            raise ValueError(
                f"lifecycle '{lifecycle}' is not one of {sorted(VALID_LIFECYCLES)}"
            )
        fields.append("lifecycle = ?"); params.append(lifecycle)
    if mode is not None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"mode '{mode}' is not one of {sorted(VALID_MODES)}"
            )
        fields.append("mode = ?"); params.append(mode)
    if priority is not None:
        fields.append("priority = ?"); params.append(int(priority))
    if on_timeout is not None:
        if on_timeout not in VALID_ON_TIMEOUT:
            raise ValueError(
                f"on_timeout '{on_timeout}' is not one of {sorted(VALID_ON_TIMEOUT)}"
            )
        fields.append("on_timeout = ?"); params.append(on_timeout)
    if on_internal_error is not None:
        if on_internal_error not in VALID_ON_INTERNAL_ERROR:
            raise ValueError(
                f"on_internal_error '{on_internal_error}' is not one of "
                f"{sorted(VALID_ON_INTERNAL_ERROR)}"
            )
        fields.append("on_internal_error = ?"); params.append(on_internal_error)
    if circuit_breaker_state is not None:
        if circuit_breaker_state not in {"ok", "tripped"}:
            raise ValueError(
                f"circuit_breaker_state '{circuit_breaker_state}' must be 'ok' or 'tripped'"
            )
        fields.append("circuit_breaker_state = ?"); params.append(circuit_breaker_state)

    fields.append("version = version + 1")
    fields.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).replace(tzinfo=None))
    params.append(name)

    db.execute(
        f"UPDATE policies SET {', '.join(fields)} WHERE name = ?",
        params,
    )
    after = db.fetchone_dict("SELECT * FROM policies WHERE name = ?", [name])
    _audit(db, name, audit_action, before=before, after=after, actor=actor)
    # Slice 6C — snapshot the post-update policy into policy_versions
    # so /v1/policies/{name}/versions has a complete audit-trail of
    # what the rule looked like at every point in time. The snapshot
    # uses the new ``version`` column we just bumped, so each row
    # has a contiguous integer version_number.
    _snapshot_version(db, name, actor=actor)
    saved = _row_to_policy(after) if after else None
    assert saved is not None
    return saved


def delete_policy(db: Database, name: str, actor: Optional[str] = None) -> bool:
    """Soft-delete (enabled=false). Returns True if a row was disabled,
    False if no enabled row existed.

    We don't hard-delete because past violations reference policy_name
    in the audit/history view; nuking the row would orphan that text.
    """
    before = db.fetchone_dict(
        "SELECT * FROM policies WHERE name = ? AND enabled = true", [name]
    )
    if before is None:
        return False
    db.execute(
        "UPDATE policies SET enabled = false, version = version + 1, "
        "updated_at = ? WHERE name = ?",
        [datetime.now(timezone.utc).replace(tzinfo=None), name],
    )
    after = db.fetchone_dict("SELECT * FROM policies WHERE name = ?", [name])
    _audit(db, name, "delete", before=before, after=after, actor=actor)
    return True


# ---- audit ----------------------------------------------------------------


def _audit(
    db: Database,
    policy_name: str,
    action: str,
    *,
    before: Optional[dict],
    after: Optional[dict],
    actor: Optional[str],
) -> None:
    """Append an audit log row. Failure here is logged + swallowed —
    a flaky audit write must not abort the policy CRUD operation."""
    try:
        db.execute(
            """
            INSERT INTO policy_audit (policy_name, action, before, after, actor)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                policy_name,
                action,
                json.dumps(_safe_audit_payload(before)) if before is not None else None,
                json.dumps(_safe_audit_payload(after)) if after is not None else None,
                actor,
            ],
        )
    except Exception:
        logger.exception("policy_audit: write failed for %s/%s", policy_name, action)


def _snapshot_version(
    db: Database,
    policy_name: str,
    *,
    actor: Optional[str] = None,
) -> Optional[int]:
    """Snapshot the current policy row into ``policy_versions`` after
    a successful create / update / rollback. Returns the version_number
    written, or None if anything went wrong (Rule 7 — never let a
    snapshot failure block the underlying CRUD).

    The snapshot's ``version_number`` mirrors the ``version`` column on
    ``policies`` so operators can refer to the same number across
    audit / history surfaces.
    """
    try:
        row = db.fetchone_dict(
            "SELECT * FROM policies WHERE name = ?", [policy_name],
        )
        if row is None:
            return None
        version_number = int(row.get("version") or 1)
        # YAML serialise — fall back to JSON if pyyaml chokes on a
        # weird value. The yaml column is text either way.
        try:
            import yaml
            payload_dict = _safe_audit_payload(row) or {}
            yaml_blob = yaml.safe_dump(payload_dict, sort_keys=True, default_flow_style=False)
        except Exception:
            yaml_blob = json.dumps(_safe_audit_payload(row), default=str)

        version_id = (
            f"{policy_name}|{version_number}|"
            f"{datetime.now(timezone.utc).timestamp()}"
        )
        db.execute(
            """
            INSERT INTO policy_versions (
                id, policy_id, version_number, yaml, created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                version_id,
                policy_name,
                version_number,
                yaml_blob,
                datetime.now(timezone.utc).replace(tzinfo=None),
                actor,
            ],
        )
        return version_number
    except Exception:
        logger.exception(
            "policy_versions: snapshot failed for %s", policy_name,
        )
        return None


def list_versions(
    db: Database,
    policy_name: str,
    *,
    limit: int = 100,
) -> List[dict]:
    """Return version snapshots for ``policy_name``, newest first.

    Each row carries id, version_number, yaml (full policy at that
    point in time), created_at, created_by.
    """
    rows = db.fetchall_dict(
        """
        SELECT * FROM policy_versions
        WHERE policy_id = ?
        ORDER BY version_number DESC
        LIMIT ?
        """,
        [policy_name, limit],
    )
    return [
        {
            "id": r["id"],
            "version_number": int(r.get("version_number") or 0),
            "yaml": r.get("yaml") or "",
            "created_at": r.get("created_at"),
            "created_by": r.get("created_by"),
        }
        for r in rows
    ]


def get_version(
    db: Database, policy_name: str, version_number: int,
) -> Optional[dict]:
    row = db.fetchone_dict(
        """
        SELECT * FROM policy_versions
        WHERE policy_id = ? AND version_number = ?
        """,
        [policy_name, version_number],
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "version_number": int(row.get("version_number") or 0),
        "yaml": row.get("yaml") or "",
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by"),
    }


def rollback_to_version(
    db: Database,
    policy_name: str,
    version_number: int,
    *,
    actor: Optional[str] = None,
) -> Policy:
    """Restore a policy to an earlier version. Pulls the snapshot,
    parses it, applies via update_policy() so the rollback itself
    is audited as a normal update — and itself produces a new
    version snapshot.
    """
    snap = get_version(db, policy_name, version_number)
    if snap is None:
        raise KeyError(f"policy {policy_name!r} version {version_number} not found")

    # Parse the YAML snapshot back to fields. yaml.safe_load handles
    # both YAML and JSON.
    try:
        import yaml
        parsed = yaml.safe_load(snap["yaml"]) or {}
    except Exception:
        try:
            parsed = json.loads(snap["yaml"])
        except Exception:
            raise ValueError("snapshot yaml is malformed; rollback aborted")

    # Apply via update_policy so the audit + new-snapshot path runs.
    return update_policy(
        db,
        policy_name,
        description=parsed.get("description"),
        trigger=parsed.get("trigger"),
        condition=parsed.get("condition"),
        action=parsed.get("action"),
        severity=parsed.get("severity"),
        webhook_url=parsed.get("webhook_url"),
        scope_agents=(
            parsed.get("scope_agents") if isinstance(
                parsed.get("scope_agents"), list,
            ) else None
        ),
        enabled=parsed.get("enabled"),
        lifecycle=parsed.get("lifecycle"),
        mode=parsed.get("mode"),
        priority=parsed.get("priority"),
        on_timeout=parsed.get("on_timeout"),
        on_internal_error=parsed.get("on_internal_error"),
        actor=actor or "rollback",
        audit_action="rollback",
    )


def _safe_audit_payload(d: Optional[dict]) -> Optional[dict]:
    """Strip values that don't JSON-serialize cleanly (datetime → ISO).

    DuckDB returns datetime objects for timestamp columns; json.dumps
    chokes on them. Convert in place rather than reaching for a
    custom encoder so the on-disk audit JSON is plain.
    """
    if d is None:
        return None
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def list_audit(
    db: Database, policy_name: Optional[str] = None, limit: int = 100, offset: int = 0
) -> Tuple[List[dict], int]:
    where = "WHERE policy_name = ?" if policy_name else ""
    params = [policy_name] if policy_name else []
    rows = db.fetchall_dict(
        f"""
        SELECT * FROM policy_audit
        {where}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    total_row = db.fetchone(
        f"SELECT COUNT(*) FROM policy_audit {where}", params
    )
    total = int(total_row[0]) if total_row else 0

    # Decode the JSON columns so the API can return structured objects.
    for r in rows:
        for k in ("before", "after"):
            v = r.get(k)
            if isinstance(v, str):
                try:
                    r[k] = json.loads(v)
                except json.JSONDecodeError:
                    pass
    return rows, total


# ---- bootstrap ------------------------------------------------------------


def bootstrap_from_yaml_if_empty(db: Database, yaml_path: Optional[str]) -> int:
    """One-shot import of YAML rules into the DB on a fresh install.

    Runs only when:
      - ``yaml_path`` is set
      - the file exists
      - the policies table has zero rows

    Returns the number of rows imported. Anything else (DB has rows,
    no YAML configured) is a no-op. After this runs once, the YAML
    file is informational only — edits don't affect the engine.
    """
    if not yaml_path:
        return 0
    if has_any_policies(db):
        return 0
    path = Path(yaml_path)
    if not path.exists():
        return 0
    try:
        engine = load_policy_engine(yaml_path)
    except PolicyConfigError as e:
        logger.warning(
            "policy: bootstrap from %s skipped (invalid YAML): %s", yaml_path, e
        )
        return 0
    if engine is None:
        return 0
    imported = 0
    for p in engine.policies:
        try:
            create_policy(db, p, actor="bootstrap")
            imported += 1
        except ValueError:
            # Race: someone else already created the same name.
            # Bootstrap should never overwrite; just skip.
            continue
        except Exception:
            logger.exception("policy: failed to bootstrap %r", p.name)
    if imported:
        logger.info(
            "policy: bootstrapped %d policies from %s into DB", imported, yaml_path
        )
    return imported


# ---- engine assembly ------------------------------------------------------


def build_engine_from_db(db: Database) -> Optional[PolicyEngine]:
    """Construct an in-memory ``PolicyEngine`` from the DB.

    The SDK's ``PolicyEngine`` only knows how to load from a YAML
    path. Rather than serialize back to YAML, we use a small
    in-memory init path: build a temp file's worth of YAML in-memory
    and feed it to the engine via tempfile.

    Returns None when the DB has no enabled rows (= engine disabled).

    Filters to ``lifecycle == 'post_ingest'``: the SDK PolicyEngine
    evaluates spans/traces post-ingest and only knows the legacy
    builtins (len/str/int/float/abs). Firewall lifecycles
    (before_proxy_call, after_tool_call, etc.) are evaluated by
    ``firewall.decide`` instead, which has the firewall builtins
    (has_pii, looks_like_secret, …) in scope. Mixing the two would
    silently drop firewall rules from the SDK engine with a "function
    not defined" warning AND would prevent the firewall engine from
    seeing them via lifecycle filter (it does its own DB read with
    ``lifecycle = ?`` filter, so this is just for SDK-engine
    correctness).
    """
    all_policies = list_policies(db, include_disabled=False)
    policies = [
        p for p in all_policies
        if (getattr(p, "lifecycle", "post_ingest") or "post_ingest") == "post_ingest"
    ]
    if not policies:
        return None
    return _build_engine_from_policies(policies)


def _build_engine_from_policies(policies: List[Policy]) -> PolicyEngine:
    """Materialize a list of Policy dataclasses as a PolicyEngine.

    Implementation: write a temp YAML, hand its path to ``PolicyEngine``,
    then unlink. Cheaper alternatives (subclassing PolicyEngine to
    skip YAML parsing) would tie the API to SDK internals; the temp-
    file dance keeps the engine API single-source-of-truth.
    """
    import tempfile
    import yaml as _yaml  # imported here so module loads even if pyyaml is missing at import time

    payload = {
        "version": 1,
        "policies": [
            {
                "name": p.name,
                "description": p.description,
                "trigger": p.trigger,
                "condition": p.condition,
                "action": p.action,
                "severity": p.severity,
                **({"webhook_url": p.webhook_url} if p.webhook_url else {}),
                **({"scope": {"agents": list(p.scope_agents)}} if p.scope_agents else {}),
            }
            for p in policies
        ],
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        _yaml.safe_dump(payload, f, sort_keys=False)
        tmp_path = f.name
    try:
        return PolicyEngine(tmp_path)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
