"""Admin / ops endpoints (Slice 5C).

Surfaces operators need on a deployed Lumin that aren't part of the
ingest / read API:

  - GET  /v1/admin/health                    deeper than /health —
                                              per-component status
  - GET  /v1/admin/retention                 current settings
  - POST /v1/admin/retention/cleanup         run cleanup once with
                                              an explicit cutoff
  - GET  /v1/admin/backups                   list snapshots
  - POST /v1/admin/backups                   create a snapshot
  - POST /v1/admin/backups/{name}/restore    overwrite live DB

Backup / restore strategy: DuckDB ``EXPORT DATABASE`` / ``IMPORT
DATABASE`` to a directory. Each snapshot lives at
``LUMIN_BACKUP_DIR/<name>/`` (default
``$LUMIN_DATA_DIR/backups/<name>/``). Names are restricted to
``[a-z0-9_-]`` to prevent path-traversal.

These endpoints are guarded by the same ``LUMIN_API_TOKEN``
middleware (Slice 5B). Restores in particular are destructive; the
endpoint requires an explicit ``confirm: true`` field in the
request body so a curl typo can't wipe production state.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import Database, get_db

logger = logging.getLogger("lumin.api.routers.admin")

router = APIRouter()


# ---- helpers --------------------------------------------------------------


_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _backup_dir() -> Path:
    """Resolve the backup root from env. Defaults to a 'backups'
    sibling of the configured data dir, which is what docker-compose
    operators get out of the box."""
    raw = os.environ.get("LUMIN_BACKUP_DIR", "").strip()
    if raw:
        return Path(raw)
    data_dir = Path(os.environ.get("LUMIN_DATA_DIR", "data"))
    return data_dir / "backups"


def _validate_name(name: str) -> str:
    if not _NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "name must match [a-z0-9][a-z0-9_-]{0,63}; got "
                f"{name!r}"
            ),
        )
    return name


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- /v1/admin/health -----------------------------------------------------


class HealthComponent(BaseModel):
    name: str
    status: str  # "ok" | "degraded" | "down"
    detail: Optional[str] = None


class AdminHealth(BaseModel):
    status: str
    started_at: Optional[datetime] = None
    components: List[HealthComponent]


@router.get("/v1/admin/health", response_model=AdminHealth)
def admin_health(db: Database = Depends(get_db)) -> AdminHealth:
    """Per-component health for the dashboard's ops page.

    Dashboard's existing ``/health`` is a 200/200 binary — useful
    for the Docker healthcheck, useless when something is degraded
    but technically responding. This breaks it down per subsystem.
    """
    components: List[HealthComponent] = []

    # Database
    try:
        row = db.fetchone("SELECT 1")
        components.append(
            HealthComponent(
                name="database",
                status="ok" if row else "degraded",
                detail=None,
            )
        )
    except Exception as e:
        components.append(
            HealthComponent(name="database", status="down", detail=str(e)[:200])
        )

    # Firewall engine
    try:
        import policy_runtime

        policies = policy_runtime.get_engine().policies
        components.append(
            HealthComponent(
                name="policy_engine",
                status="ok",
                detail=f"{len(policies)} policy(ies) loaded",
            )
        )
    except Exception as e:
        components.append(
            HealthComponent(
                name="policy_engine", status="degraded", detail=str(e)[:200],
            )
        )

    # Webhook DLQ — not a hard failure, but operators want to see it
    try:
        dlq_row = db.fetchone(
            "SELECT COUNT(*) FROM firewall_webhook_failures"
        )
        dlq_count = int(dlq_row[0]) if dlq_row else 0
        components.append(
            HealthComponent(
                name="webhook_dlq",
                status="ok" if dlq_count == 0 else "degraded",
                detail=f"{dlq_count} failed deliveries",
            )
        )
    except Exception:
        # Table may not exist yet on older DBs
        components.append(
            HealthComponent(
                name="webhook_dlq", status="ok", detail="(table absent)",
            )
        )

    # Backup dir
    bd = _backup_dir()
    if bd.exists() and os.access(bd, os.W_OK):
        components.append(
            HealthComponent(
                name="backup_dir", status="ok", detail=str(bd),
            )
        )
    else:
        components.append(
            HealthComponent(
                name="backup_dir",
                status="degraded",
                detail=f"{bd} missing or not writable",
            )
        )

    overall = "ok"
    if any(c.status == "down" for c in components):
        overall = "down"
    elif any(c.status == "degraded" for c in components):
        overall = "degraded"
    return AdminHealth(status=overall, components=components)


# ---- /v1/admin/retention --------------------------------------------------


class RetentionConfig(BaseModel):
    days: int
    cleanup_enabled: bool
    cleanup_interval_hours: int
    backup_dir: str


class RetentionCleanupRequest(BaseModel):
    days: int = Field(..., ge=0, le=3650)


class RetentionCleanupResponse(BaseModel):
    deleted_traces: int
    cutoff: datetime


@router.get("/v1/admin/retention", response_model=RetentionConfig)
def get_retention() -> RetentionConfig:
    days = int(os.environ.get("LUMIN_RETENTION_DAYS", "90"))
    cleanup_enabled = os.environ.get("LUMIN_CLEANUP_ENABLED", "true").lower() in (
        "1", "true", "yes",
    )
    interval = int(os.environ.get("LUMIN_CLEANUP_INTERVAL_HOURS", "24"))
    return RetentionConfig(
        days=days,
        cleanup_enabled=cleanup_enabled,
        cleanup_interval_hours=interval,
        backup_dir=str(_backup_dir()),
    )


@router.post(
    "/v1/admin/retention/cleanup", response_model=RetentionCleanupResponse,
)
def run_retention_cleanup(
    payload: RetentionCleanupRequest,
    db: Database = Depends(get_db),
) -> RetentionCleanupResponse:
    """Run the cleanup task once with an explicit cutoff.

    Useful for operators who want to trim a runaway DB without
    waiting for the next scheduled tick (default daily). The env
    var ``LUMIN_RETENTION_DAYS`` is unaffected — this is one-shot.
    """
    deleted = db.cleanup_old_traces(payload.days)
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=payload.days)).replace(
        tzinfo=None,
    )
    return RetentionCleanupResponse(deleted_traces=deleted, cutoff=cutoff)


# ---- /v1/admin/backups ----------------------------------------------------


class BackupSummary(BaseModel):
    name: str
    path: str
    created_at: datetime
    size_bytes: int


class BackupListResponse(BaseModel):
    backup_dir: str
    backups: List[BackupSummary]


class BackupCreateRequest(BaseModel):
    name: Optional[str] = None


class BackupCreateResponse(BaseModel):
    name: str
    path: str
    size_bytes: int
    created_at: datetime


class RestoreRequest(BaseModel):
    confirm: bool = Field(
        ...,
        description=(
            "MUST be true. Required acknowledgement that restore "
            "overwrites the live database."
        ),
    )


@router.get("/v1/admin/backups", response_model=BackupListResponse)
def list_backups() -> BackupListResponse:
    bd = _backup_dir()
    backups: List[BackupSummary] = []
    if bd.exists():
        for entry in sorted(bd.iterdir()):
            if not entry.is_dir():
                continue
            try:
                created = datetime.fromtimestamp(entry.stat().st_mtime)
                size = sum(
                    p.stat().st_size for p in entry.rglob("*") if p.is_file()
                )
            except Exception:
                continue
            backups.append(
                BackupSummary(
                    name=entry.name,
                    path=str(entry),
                    created_at=created,
                    size_bytes=size,
                )
            )
    return BackupListResponse(backup_dir=str(bd), backups=backups)


@router.post("/v1/admin/backups", response_model=BackupCreateResponse)
def create_backup(
    payload: BackupCreateRequest,
    db: Database = Depends(get_db),
) -> BackupCreateResponse:
    """Snapshot the DuckDB to a named directory under LUMIN_BACKUP_DIR.

    Default name is the UTC timestamp (``snap_YYYYMMDDTHHMMSSZ``)
    so unattended cron jobs don't have to think up names.
    """
    if payload.name:
        name = _validate_name(payload.name)
    else:
        name = "snap_" + _utc_now().strftime("%Y%m%dT%H%M%SZ")

    bd = _backup_dir()
    bd.mkdir(parents=True, exist_ok=True)
    target = bd / name
    if target.exists():
        raise HTTPException(
            status_code=409,
            detail=f"backup {name!r} already exists",
        )
    target.mkdir(parents=True)

    # DuckDB EXPORT DATABASE writes a per-table CSV + a load.sql
    # script. Single statement, atomic relative to other writers
    # (the connection's write lock is held for the duration).
    try:
        db.execute(f"EXPORT DATABASE '{target}' (FORMAT CSV)")
    except Exception as e:
        # Roll back the empty directory so a partial export doesn't
        # confuse the listing.
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(
            status_code=500, detail=f"EXPORT DATABASE failed: {e}",
        )

    size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    return BackupCreateResponse(
        name=name,
        path=str(target),
        size_bytes=size,
        created_at=_utc_now(),
    )


@router.post("/v1/admin/backups/{name}/restore")
def restore_backup(
    name: str,
    payload: RestoreRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Restore the named snapshot. **Destructive** — overwrites
    every table in the live DB.

    Caller must pass ``{"confirm": true}``. Anything else 400s so a
    misfired curl can't accidentally roll back the database.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "confirm must be true. This endpoint overwrites the "
                "live database — re-send with {\"confirm\": true} "
                "if that's intended."
            ),
        )
    name = _validate_name(name)
    target = _backup_dir() / name
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"backup {name!r} not found")

    try:
        # IMPORT DATABASE recreates each table from the snapshot's
        # CREATE statements — it doesn't merge. We DROP existing
        # tables first so the import can re-create them cleanly.
        # Re-run firewall migrations after the import so any new
        # indexes / columns added since the snapshot was taken
        # land on the restored tables (idempotent).
        for table in (
            "policy_violations", "evals", "spans", "traces",
            "decisions", "approvals", "labels",
            "pattern_suggestions", "policy_versions",
            "firewall_webhooks", "firewall_webhook_failures",
            "firewall_kv", "policies", "policy_audit",
        ):
            try:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                pass
        db.execute(f"IMPORT DATABASE '{target}'")
        # Re-apply migrations so any post-snapshot schema additions
        # land on the restored tables.
        try:
            from firewall import migrations as _fw_migrations
            _fw_migrations.apply(db._duck)
        except Exception:
            logger.exception("admin: post-restore migrations failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"IMPORT DATABASE failed: {e}")

    return {"restored": name, "from": str(target)}


@router.delete("/v1/admin/backups/{name}")
def delete_backup(name: str) -> Dict[str, Any]:
    name = _validate_name(name)
    target = _backup_dir() / name
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"backup {name!r} not found")
    try:
        shutil.rmtree(target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to delete: {e}")
    return {"deleted": name}
