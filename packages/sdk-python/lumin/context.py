import contextvars
from typing import TYPE_CHECKING, Optional

from .span import Span

if TYPE_CHECKING:
    from .session import Session

_current_span: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar(
    "lumin_current_span", default=None
)
_current_session: contextvars.ContextVar[Optional["Session"]] = contextvars.ContextVar(
    "lumin_current_session", default=None
)


def get_current_span() -> Optional[Span]:
    return _current_span.get()


def set_current_span(span: Optional[Span]) -> contextvars.Token:
    return _current_span.set(span)


def reset_current_span(token: contextvars.Token) -> None:
    _current_span.reset(token)


def get_current_session() -> Optional["Session"]:
    return _current_session.get()
