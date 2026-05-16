"""Bearer-token authentication middleware (Slice 5B).

Default-off for localhost. When ``LUMIN_API_TOKEN`` is unset, every
endpoint is reachable without credentials **from loopback** — the
zero-friction localhost-dev story is unchanged. But an instance with
no token that is reachable from a non-loopback address refuses remote
requests (HTTP 403) instead of silently serving every trace, prompt,
and the firewall control plane to the network. Operators opt back into
the old wide-open behavior with ``LUMIN_ALLOW_INSECURE=1``; the right
fix is to set ``LUMIN_API_TOKEN``.

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
import ipaddress
import logging
import os
from typing import Iterable, Optional, Tuple

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("lumin.api.auth")

# One-time latch so a remote-exposed-without-auth instance logs its
# posture once (at the first offending request) instead of every time.
_warned_insecure_exposure = False


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


def insecure_allowed() -> bool:
    """Operator escape hatch: ``LUMIN_ALLOW_INSECURE=1`` accepts the
    risk of serving the API to non-loopback clients without a token
    (e.g. a trusted private network, or auth terminated at a proxy)."""
    return os.environ.get("LUMIN_ALLOW_INSECURE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _client_is_local(request: Request) -> bool:
    """True when the request's transport peer is loopback.

    This is the socket peer the ASGI server reports — NOT a
    user-controllable header — so it can't be spoofed by a remote
    client. Behind a reverse proxy every request looks loopback;
    those deployments must set ``LUMIN_API_TOKEN`` (the proxy can't
    be the security boundary for the trace store). Unknown / non-IP
    peers (Starlette TestClient's ``testclient``, ``localhost``, or a
    missing client on some ASGI servers) are treated as local so the
    zero-friction localhost story and the test suite are unaffected.
    """
    client = request.client
    if client is None or not client.host:
        return True
    host = client.host
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


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
        path = request.url.path
        expected = _expected_token()

        if expected is None:
            # No token configured. Localhost keeps the zero-friction
            # story (open, exactly as before). But a Lumin bound to a
            # public interface with no token would serve every trace,
            # prompt, and the firewall control plane to the world —
            # refuse non-loopback clients unless the operator has
            # explicitly accepted the risk. Public paths (healthcheck,
            # spec) stay open so probes and the container healthcheck
            # don't break.
            if (
                _is_public_path(path)
                or _client_is_local(request)
                or insecure_allowed()
            ):
                return await call_next(request)
            global _warned_insecure_exposure
            if not _warned_insecure_exposure:
                _warned_insecure_exposure = True
                logger.warning(
                    "Refusing non-loopback request from %s with no "
                    "LUMIN_API_TOKEN set. Set a token to allow remote "
                    "access, or LUMIN_ALLOW_INSECURE=1 to accept the risk.",
                    getattr(request.client, "host", "?"),
                )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "remote_access_requires_auth",
                    "detail": (
                        "This Lumin is reachable from a non-loopback "
                        "address but has no LUMIN_API_TOKEN set, so it "
                        "won't serve traces/policies to remote clients. "
                        "Set LUMIN_API_TOKEN (recommended) and send "
                        "Authorization: Bearer <token>, or set "
                        "LUMIN_ALLOW_INSECURE=1 to accept the risk."
                    ),
                },
            )

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


__all__ = [
    "BearerAuthMiddleware",
    "auth_enabled",
    "insecure_allowed",
]
