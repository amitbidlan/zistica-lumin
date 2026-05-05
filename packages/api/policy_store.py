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
    }


def _row_to_policy(row: dict) -> Policy:
    """Construct a Policy from a ``policies`` row.

    Tolerates both JSON-encoded scope_agents (DB read) and an already-
    decoded list (test fixtures or manual inserts).
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
    )


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
    """
    row = db.fetchone(
        "SELECT COUNT(*), COALESCE(MAX(version), 0) FROM policies WHERE enabled = true"
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
            actor=actor,
            audit_action="create",
        )

    row = _policy_to_row(p)
    db.execute(
        """
        INSERT INTO policies (
            name, description, trigger, condition, action, severity,
            webhook_url, scope_agents, enabled, version,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        [
            row["name"], row["description"], row["trigger"], row["condition"],
            row["action"], row["severity"], row["webhook_url"],
            row["scope_agents"], row["enabled"],
            datetime.now(timezone.utc).replace(tzinfo=None),
            datetime.now(timezone.utc).replace(tzinfo=None),
        ],
    )
    _audit(db, p.name, "create", before=None, after=row, actor=actor)
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
    """
    policies = list_policies(db, include_disabled=False)
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
