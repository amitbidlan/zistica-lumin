import asyncio
from typing import List, Optional

import httpx

from .span import Span


class HTTPExporter:
    """Async HTTP exporter. POSTs spans to {host}/v1/spans. Never raises."""

    def __init__(
        self,
        host: str,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
        project: Optional[str] = None,
    ):
        self._url = host.rstrip("/") + "/v1/spans"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key
        # Forward the configured project as the X-Lumin-Project header so
        # the API can group agents by framework on the dashboard. The TS
        # exporters do this; the Python SDK was missing the wire-up,
        # leaving every Python agent stuck under "default" in the agent
        # grid even when configure(project="my-bot") was called.
        if project:
            self._headers["X-Lumin-Project"] = project
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _ensure_client(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def export(self, spans: List[Span]) -> None:
        if not spans:
            return
        try:
            self._ensure_client()
            payload = {"spans": [s.to_dict() for s in spans]}
            async with asyncio.timeout(self._timeout):
                await self._client.post(self._url, json=payload, headers=self._headers)
        except Exception:
            # Drop silently — agent must never see exporter errors.
            pass

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
