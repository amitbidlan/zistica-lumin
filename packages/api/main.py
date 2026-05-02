import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from db import Database, get_db
from routers import evals, spans, traces

logger = logging.getLogger("synaptic.api")


# --- retention config ---


def _retention_days() -> int:
    try:
        return max(0, int(os.environ.get("SYNAPTIC_RETENTION_DAYS", "90")))
    except ValueError:
        return 90


def _cleanup_interval_seconds() -> int:
    try:
        hours = max(1, int(os.environ.get("SYNAPTIC_CLEANUP_INTERVAL_HOURS", "24")))
    except ValueError:
        hours = 24
    return hours * 3600


def _cleanup_enabled() -> bool:
    return os.environ.get("SYNAPTIC_CLEANUP_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
    )


async def _cleanup_loop(db: Database, retention_days: int, interval_seconds: int) -> None:
    """Background task: every `interval_seconds`, delete traces older than
    `retention_days`. Failures are logged but never crash the loop."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            count = db.cleanup_old_traces(retention_days)
            if count > 0:
                logger.info(
                    "retention cleanup: deleted %d traces older than %d days",
                    count,
                    retention_days,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("retention cleanup failed (will retry next interval)")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    cleanup_task: asyncio.Task[None] | None = None
    if _cleanup_enabled():
        try:
            cleanup_task = asyncio.create_task(
                _cleanup_loop(get_db(), _retention_days(), _cleanup_interval_seconds())
            )
        except Exception:
            logger.exception("could not start retention cleanup task")

    yield

    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="Synaptic API",
    description="Local-first AI agent observability — ingest and query API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(spans.router)
app.include_router(traces.router)
app.include_router(evals.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
