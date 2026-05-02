import contextvars
from typing import Optional

from .span import Span

_current_span: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar(
    "synaptic_current_span", default=None
)


def get_current_span() -> Optional[Span]:
    return _current_span.get()


def set_current_span(span: Optional[Span]) -> contextvars.Token:
    return _current_span.set(span)


def reset_current_span(token: contextvars.Token) -> None:
    _current_span.reset(token)
