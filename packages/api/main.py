import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import policy_runtime
from db import Database, get_db
from routers import agents, evals, firewall, otlp, policy, proxy, sessions, spans, traces
from ws import manager as ws_manager

logger = logging.getLogger("lumin.api")


# --- retention config ---


def _retention_days() -> int:
    try:
        return max(0, int(os.environ.get("LUMIN_RETENTION_DAYS", "90")))
    except ValueError:
        return 90


def _cleanup_interval_seconds() -> int:
    try:
        hours = max(1, int(os.environ.get("LUMIN_CLEANUP_INTERVAL_HOURS", "24")))
    except ValueError:
        hours = 24
    return hours * 3600


def _cleanup_enabled() -> bool:
    return os.environ.get("LUMIN_CLEANUP_ENABLED", "true").lower() in (
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


async def _policy_watch_loop(interval_seconds: int = 30) -> None:
    """Background task: keep the in-memory engine in sync with its
    source of truth.

    Both watchers run on every tick:
      - DB-token watcher (Phase 4): cheap (row_count + max_version)
        check; reloads when CRUD edits land. This is the primary
        path once policies are managed via the dashboard.
      - YAML mtime watcher (Phase 1): only useful when the engine
        is sourced from YAML. No-ops when DB is authoritative.

    Operators can also POST /v1/policy/reload for an immediate
    force-reload. Errors are logged and swallowed; a transient blip
    must never crash the API.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            # DB first — it's the cheaper / more common path. If it
            # reloads we're already up to date; otherwise the mtime
            # check might still pick up a YAML edit on a fallback
            # deployment.
            reloaded = policy_runtime.maybe_reload_on_db_token_change()
            if not reloaded:
                policy_runtime.maybe_reload_on_mtime_change()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("policy watcher failed (will retry)")


def _policy_watch_enabled() -> bool:
    return os.environ.get("LUMIN_POLICY_WATCH", "true").lower() in ("1", "true", "yes")


def _policy_watch_interval() -> int:
    try:
        return max(1, int(os.environ.get("LUMIN_POLICY_WATCH_INTERVAL", "30")))
    except ValueError:
        return 30


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Register the running event loop so sync handlers (which run in the
    # threadpool) can schedule WebSocket broadcasts on it via
    # asyncio.run_coroutine_threadsafe.
    ws_manager.set_loop(asyncio.get_event_loop())

    cleanup_task: asyncio.Task[None] | None = None
    if _cleanup_enabled():
        try:
            cleanup_task = asyncio.create_task(
                _cleanup_loop(get_db(), _retention_days(), _cleanup_interval_seconds())
            )
        except Exception:
            logger.exception("could not start retention cleanup task")

    # Phase 4 — one-shot YAML→DB bootstrap. Runs only when the policies
    # table is empty AND LUMIN_POLICY_FILE is set; subsequent restarts
    # see the now-populated DB and skip this entirely. After bootstrap
    # the engine reads from the DB and YAML edits are informational.
    try:
        import policy_store
        bootstrapped = policy_store.bootstrap_from_yaml_if_empty(
            get_db(), os.environ.get("LUMIN_POLICY_FILE")
        )
        if bootstrapped:
            logger.info("policy: bootstrap imported %d policies from YAML", bootstrapped)
    except Exception:
        logger.exception("policy bootstrap failed (continuing — engine falls back to YAML)")

    # Agent Firewall — auto-install the OWASP LLM Top 10 starter pack
    # the first time the API comes up against an empty policies table.
    # All rules ship in mode=shadow per §10.1 of AGENT_FIREWALL_SPEC.md
    # so a fresh install never blocks live traffic on day one.
    try:
        from firewall.starter_packs import bootstrap as _starter
        installed = _starter.install_owasp_pack_if_fresh(get_db())
        if installed:
            logger.info(
                "firewall: starter pack imported %d OWASP rules (shadow mode)",
                installed,
            )
    except Exception:
        logger.exception("firewall starter pack install failed (continuing)")

    # Eagerly load the policy engine at startup so metrics/snapshot
    # reflect "loaded N policies" without waiting for the first ingest.
    # Errors fall through to the in-process logger; we don't fail
    # startup over a broken policy file.
    try:
        policy_runtime.get_engine()
    except Exception:
        logger.exception("policy engine eager-load failed (continuing)")

    # Pull the firewall panic-disable bit forward across restarts so
    # an operator who flipped the kill-switch yesterday isn't surprised
    # by a hot engine after a deploy. Best-effort: a missing table or
    # row leaves the cached flag at False, which is the right default.
    try:
        from firewall import decide as _fw_decide
        _fw_decide.refresh_panic_state(get_db())
    except Exception:
        logger.exception("firewall panic state refresh failed (continuing)")

    policy_watch_task: asyncio.Task[None] | None = None
    if _policy_watch_enabled():
        try:
            policy_watch_task = asyncio.create_task(
                _policy_watch_loop(_policy_watch_interval())
            )
        except Exception:
            logger.exception("could not start policy mtime watcher")

    yield

    if cleanup_task is not None:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):
            pass
    if policy_watch_task is not None:
        policy_watch_task.cancel()
        try:
            await policy_watch_task
        except (asyncio.CancelledError, Exception):
            pass

    # Checkpoint + close the DuckDB connection on graceful shutdown.
    # Without this, the WAL stays unmerged on disk; the next start
    # tries to replay it and hits an internal "GetDefaultDatabase
    # with no default database set" assertion that wedges the API.
    # This was the source of every "corrupt WAL" stall during the
    # phase 1-4 build. SIGKILL still leaves a stale WAL — that's a
    # DuckDB limitation — but SIGTERM / SIGINT (Ctrl-C, supervisord
    # stop, a plain `kill <pid>`) now flushes cleanly.
    #
    # NB: we reset ``db._db`` to None after close so a follow-up
    # lifespan (TestClient reuses the singleton across tests) re-
    # initializes fresh. Without this reset, every test after the
    # first would hit "Connection already closed" via the stale
    # _db singleton.
    try:
        import db as _db_module
        with _db_module._db_lock:
            current = _db_module._db
            if current is not None:
                try:
                    current.execute("CHECKPOINT")
                except Exception:
                    logger.exception("db: CHECKPOINT failed at shutdown (continuing)")
                current.close()
                _db_module._db = None
                logger.info("db: closed cleanly on shutdown")
    except Exception:
        logger.exception("db: shutdown close failed")


app = FastAPI(
    title="Lumin API",
    description="Local-first AI agent observability — ingest and query API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(spans.router)
app.include_router(traces.router)
app.include_router(sessions.router)
app.include_router(evals.router)
app.include_router(policy.router)
app.include_router(agents.router)
app.include_router(otlp.router)
app.include_router(proxy.router)
app.include_router(firewall.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.websocket("/ws/traces")
async def ws_traces(websocket: WebSocket) -> None:
    """Real-time trace + span fanout. The dashboard subscribes here and
    receives ``new_trace`` / ``new_span`` messages as agents post spans.
    Server-to-client only — incoming messages are ignored (we still drain
    them so the socket stays alive)."""
    await ws_manager.connect(websocket)
    try:
        # Block until the client disconnects. We don't expect inbound
        # messages, but draining keeps the socket honest.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket handler error")
    finally:
        await ws_manager.disconnect(websocket)
