"""Bearer-token authentication middleware (Slice 5B).

Default-off. When ``LUMIN_API_TOKEN`` is unset, the middleware is a
no-op and the API behaves exactly like it did before — every
endpoint is reachable without credentials. This preserves the
zero-friction localhost-dev story.

When ``LUMIN_API_TOKEN`` is set, every request to ``/v1/*``,
``/ws/*``, and ``/health/admin`` requires:

    Authorization: Bearer <LUMIN_API_TOKEN>

…or, for WebSocket connections that can't carry headers easily,
a query string ``?token=<LUMIN_API_TOKEN>``.

Exempt paths (always reachable, no token required):
  - ``/health``         the container healthcheck hits this
  - ``/openapi.json``   FastAPI's spec; needed by tooling
  - ``/docs``, ``/redoc``  same
  - ``/`` (root)        a minimal JSON identity card

Why a custom middleware instead of FastAPI's ``HTTPBearer`` /
``Security``? Two reasons:

  1. Token comparison must be **constant-time** to avoid timing
     attacks. ``hmac.compare_digest`` does that; the FastAPI
     security helpers don't.
  2. The off-by-default story needs to be a single env-var check
     — wrapping every router with a Depends would force every
     test to opt out individually.

The middleware is added in ``main.py`` after all routers are
registered. Order matters — auth must run BEFORE the router
matches the path, so it has a chance to 401 before the handler
fires.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Iterable, Optional, Tuple

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("lumin.api.auth")


# Paths that are ALWAYS reachable without a token. Order doesn't
# matter — startswith match.
_PUBLIC_PREFIXES: Tuple[str, ...] = (
    "/health",
    "/openapi.json",
    "/docs",
    "/redoc",
)
_PUBLIC_EXACT: Tuple[str, ...] = ("/",)


def _expected_token() -> Optional[str]:
    """Return the configured API token, or None if auth is disabled.

    Read at request time (not at module load) so an operator can
    rotate the token via env without restarting — the next request
    picks up the new value.
    """
    raw = os.environ.get("LUMIN_API_TOKEN", "")
    return raw.strip() or None


def auth_enabled() -> bool:
    return _expected_token() is not None


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for prefix in _PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix):
            return True
    return False


def _extract_token(request: Request) -> Optional[str]:
    """Pull the candidate token off the request.

    Header preferred (standard ``Authorization: Bearer <token>``),
    query-string fallback for WebSocket connections that can't
    cleanly attach headers (browsers, especially)."""
    auth_header = request.headers.get("authorization") or ""
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
    qs_token = request.query_params.get("token")
    if qs_token:
        return qs_token.strip() or None
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate every non-public request against ``LUMIN_API_TOKEN``.

    Returns 401 when the token is missing, 403 when it's present but
    wrong. The split lets operators distinguish "did the client
    forget the header?" from "did the rotated token get pushed?"."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        expected = _expected_token()
        if expected is None:
            return await call_next(request)

        path = request.url.path
        if _is_public_path(path):
            return await call_next(request)

        provided = _extract_token(request)
        if not provided:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "missing_authorization",
                    "detail": (
                        "LUMIN_API_TOKEN is set on this Lumin instance. "
                        "Send Authorization: Bearer <token>."
                    ),
                },
            )
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse(
                status_code=403,
                content={
                    "error": "invalid_token",
                    "detail": "Bearer token did not match LUMIN_API_TOKEN.",
                },
            )
        return await call_next(request)


__all__ = ["BearerAuthMiddleware", "auth_enabled"]
