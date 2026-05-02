from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class SpanInput(BaseModel):
    """Span as accepted by POST /v1/spans. Many fields are optional so that
    minimal payloads (the curl example in the session prompt) and rich
    payloads (LangChain integration) both work.
    """

    id: str
    trace_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = "custom"
    input: Optional[str] = None
    output: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None
    # Convenience field used by the SDK
    error: Optional[str] = None
    # Direct DB-shaped fields (alternative to `error`)
    error_message: Optional[str] = None
    status: Optional[str] = None
    # Optional richer fields
    model: Optional[str] = None
    provider: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    cost_usd: Optional[float] = None
    tool_name: Optional[str] = None
    metadata: Optional[dict] = None


class IngestRequest(BaseModel):
    spans: List[SpanInput]


class IngestResponse(BaseModel):
    accepted: int


class TraceCreate(BaseModel):
    id: str
    name: Optional[str] = None
    input: Optional[str] = None
    output: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None
    total_tokens: Optional[int] = 0
    total_cost_usd: Optional[float] = 0.0
    quality_score: Optional[float] = None
    user_id: Optional[str] = ""
    session_id: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[dict] = None


class Trace(BaseModel):
    id: str
    name: Optional[str] = None
    input: Optional[str] = None
    output: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    quality_score: Optional[float] = None
    user_id: str = ""
    session_id: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Any] = None
    ingest_at: Optional[datetime] = None


class Span(BaseModel):
    id: str
    trace_id: str
    parent_span_id: Optional[str] = None
    type: Optional[str] = None
    name: Optional[str] = None
    input: Optional[str] = None
    output: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    cost_usd: Optional[float] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    status: Optional[str] = "ok"
    error_message: Optional[str] = None
    tool_name: Optional[str] = None
    metadata: Optional[Any] = None


class EvalCreate(BaseModel):
    trace_id: str
    span_id: Optional[str] = None
    name: str
    score: float
    label: Optional[str] = None
    comment: Optional[str] = None
    source: Optional[str] = "manual"
    model: Optional[str] = None


class Eval(BaseModel):
    id: str
    trace_id: str
    span_id: Optional[str] = None
    name: Optional[str] = None
    score: Optional[float] = None
    label: Optional[str] = None
    comment: Optional[str] = None
    source: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[datetime] = None
