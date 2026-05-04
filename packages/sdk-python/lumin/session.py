"""Session — a logical grouping of related traces.

Use as a context manager to scope a multi-turn conversation or workflow:

    session = lumin.session(name="booking-conversation")

    with session:
        my_agent("Book me a flight to Tokyo")
        my_agent("Make it business class")
        my_agent("Add a hotel for 3 nights")

Every ``@lumin.trace`` call inside the ``with`` block automatically
gets ``session_id`` set on its root span. Sessions are derived server-side
by grouping ``traces`` rows on ``session_id`` — no separate sessions table.
"""

from __future__ import annotations

import re
import uuid
from contextvars import Token
from typing import Optional

from .context import _current_session


def _slug(name: str) -> str:
    """Make a name safe to use in a session id: lowercase, dashes only."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    return s or "session"


class Session:
    """A logical group of related traces. Both sync and async context-manager
    compatible. Pass ``id`` to use a known identifier (good for resuming a
    conversation across processes); pass ``name`` to get a slug-prefixed id;
    pass nothing for a fresh UUID."""

    def __init__(self, id: Optional[str] = None, name: Optional[str] = None):
        if id:
            self.id = id
        elif name:
            self.id = f"{_slug(name)}-{uuid.uuid4().hex[:8]}"
        else:
            self.id = str(uuid.uuid4())
        self.name = name
        self._token: Optional[Token] = None

    def __repr__(self) -> str:
        if self.name:
            return f"Session(id={self.id!r}, name={self.name!r})"
        return f"Session(id={self.id!r})"

    def __enter__(self) -> "Session":
        self._token = _current_session.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _current_session.reset(self._token)
            self._token = None

    async def __aenter__(self) -> "Session":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.__exit__(exc_type, exc, tb)


def session(id: Optional[str] = None, name: Optional[str] = None) -> Session:
    """Factory matching the ``lumin.session(...)`` lowercase form shown
    in the docs. Returns a context manager."""
    return Session(id=id, name=name)
