import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json(value: Any, max_size: int) -> Optional[str]:
    if value is None:
        return None
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        try:
            s = json.dumps(str(value), ensure_ascii=False)
        except Exception:
            s = json.dumps("<unserializable>")
    if len(s) > max_size:
        s = s[:max_size]
    return s


@dataclass
class Span:
    id: str
    trace_id: str
    parent_span_id: Optional[str]
    name: str
    type: str = "custom"
    input: Optional[str] = None
    output: Optional[str] = None
    started_at: str = field(default_factory=_now_iso)
    ended_at: Optional[str] = None
    error: Optional[str] = None
    # Optional session grouping — populated from lumin.session() context
    # or @lumin.trace(session_id=...). Default None preserves existing
    # behavior for callers that don't use sessions.
    session_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        name: str,
        type: str = "custom",
        parent: Optional["Span"] = None,
    ) -> "Span":
        new_id = str(uuid.uuid4())
        if parent is None:
            trace_id = new_id
            parent_span_id = None
        else:
            trace_id = parent.trace_id
            parent_span_id = parent.id
        return cls(
            id=new_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            name=name,
            type=type,
        )

    def end(self) -> None:
        if self.ended_at is None:
            self.ended_at = _now_iso()

    def set_input(self, value: Any, max_size: int = 10_240) -> None:
        self.input = _to_json(value, max_size)

    def set_output(self, value: Any, max_size: int = 10_240) -> None:
        self.output = _to_json(value, max_size)

    def set_error(self, exc: BaseException) -> None:
        self.error = f"{type(exc).__name__}: {exc}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "type": self.type,
            "input": self.input,
            "output": self.output,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "session_id": self.session_id,
        }


class SpanContext:
    """Context manager for a span. Used as `with lumin.span("name"):`."""

    def __init__(self, sdk, name: str, type: str = "custom"):
        self._sdk = sdk
        self._name = name
        self._type = type
        self._span: Optional[Span] = None
        self._token = None

    def __enter__(self) -> Span:
        from .context import get_current_span, set_current_span

        parent = get_current_span()
        self._span = Span.create(self._name, self._type, parent=parent)
        self._token = set_current_span(self._span)
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        from .context import reset_current_span

        assert self._span is not None
        if exc_val is not None:
            self._span.set_error(exc_val)
        self._span.end()
        self._sdk.submit(self._span)
        if self._token is not None:
            reset_current_span(self._token)
        return False
