"""WebSocket connection manager for real-time trace/span streaming.

Sync handlers (e.g. ingest_spans) call ``broadcast_threadsafe`` from a
worker thread; the message is hopped onto the running asyncio loop and
sent to every connected client. Failures (disconnected clients, slow
clients, no loop yet) are swallowed — broadcasts are best-effort and
must never affect ingest.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger("synaptic.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self._active: List[WebSocket] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_lock = asyncio.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called from lifespan startup so cross-thread scheduling has a target."""
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._async_lock:
            self._active.append(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._async_lock:
            if websocket in self._active:
                self._active.remove(websocket)

    @property
    def connection_count(self) -> int:
        return len(self._active)

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        """Send a JSON message to every connected client. Silently drops
        clients that have died — they'll be cleaned up on next disconnect."""
        if not self._active:
            return
        encoded = json.dumps(message, default=str)
        dead: List[WebSocket] = []
        async with self._async_lock:
            targets = list(self._active)
        for ws in targets:
            try:
                await ws.send_text(encoded)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._async_lock:
                for ws in dead:
                    if ws in self._active:
                        self._active.remove(ws)

    def broadcast_threadsafe(self, message: Dict[str, Any]) -> None:
        """Schedule a broadcast from a sync handler running in the threadpool.

        No-op if the event loop hasn't been set yet (e.g. during early
        startup or in tests that don't run the lifespan).
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(message), loop)
        except Exception:
            logger.exception("websocket broadcast scheduling failed")


# Module-level singleton — one connection set per process is fine for v1.
manager = ConnectionManager()
