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
    # Session grouping — propagates to the trace row when a root span lands
    session_id: Optional[str] = None
    # Claude extended-thinking support: span_subtype is "thinking" |
    # "response" | null. thinking_tokens estimates tokens spent in the
    # thinking phase (output_tokens in Anthropic usage rolls up everything,
    # so we estimate from content length).
    span_subtype: Optional[str] = None
    thinking_tokens: Optional[int] = None


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
    # Policy Engine — number of policy_violations rows linked to this
    # trace. Defaults to 0 when the engine isn't in use. Included on
    # the list endpoint so the dashboard can render violation badges
    # without an N+1 query per row.
    violation_count: int = 0


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
    span_subtype: Optional[str] = None
    thinking_tokens: Optional[int] = None
    session_id: Optional[str] = None


class Session(BaseModel):
    session_id: str
    trace_count: int = 0
    total_duration_ms: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    quality_score: Optional[float] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    # Wall-clock duration of the session — last_seen − first_seen — distinct
    # from total_duration_ms which sums per-trace durations (and may overlap
    # if turns ran concurrently).
    wall_duration_ms: Optional[int] = None


class SessionDetail(Session):
    traces: List[Trace] = []


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


# ---- Policy Engine (Accountability Layer Part B) -----------------------


class PolicyViolationInput(BaseModel):
    """One violation as accepted by POST /v1/violations from the SDK."""

    policy_name: str
    severity: str
    trace_id: str
    span_id: Optional[str] = None
    condition_text: Optional[str] = None
    action_taken: Optional[str] = None
    policy_description: Optional[str] = None
    webhook_url: Optional[str] = None
    actual_value: Optional[str] = None
    webhook_fired: Optional[bool] = False


class PolicyViolationsIngest(BaseModel):
    violations: List[PolicyViolationInput]


class PolicyViolationsIngestResponse(BaseModel):
    accepted: int


class PolicyViolation(BaseModel):
    """One violation as returned by GET /v1/violations."""

    id: str
    policy_name: str
    policy_description: Optional[str] = None
    severity: str
    trace_id: str
    span_id: Optional[str] = None
    condition_text: Optional[str] = None
    action_taken: Optional[str] = None
    actual_value: Optional[str] = None
    webhook_fired: bool = False
    webhook_url: Optional[str] = None
    created_at: Optional[datetime] = None


class PolicyViolationsResponse(BaseModel):
    violations: List[PolicyViolation]
    total: int


class PolicyViolationStats(BaseModel):
    total: int
    by_severity: dict
    by_policy: dict


class TraceViolationSummary(BaseModel):
    """Compact form embedded in GET /v1/traces/{id} responses."""

    policy_name: str
    severity: str


class TraceDetail(Trace):
    """Trace shape returned by GET /v1/traces/{id} — same as Trace
    plus a compact list of policy violations attached to the trace
    (empty when the Policy Engine isn't in use). The list endpoint
    /v1/traces stays on the simpler Trace shape so its payload size
    is unchanged."""

    policy_violations: List[TraceViolationSummary] = []
    has_violations: bool = False


# ---- Policy CRUD (Phase 3 read-only + Phase 4 writes) -------------------


class PolicyOut(BaseModel):
    """Policy as returned by GET /v1/policies + GET /v1/policies/{name}.

    Mirrors the YAML/DB shape but is the only schema that crosses the
    API boundary — the SDK's internal ``Policy`` dataclass stays in
    the SDK.
    """

    name: str
    description: Optional[str] = None
    trigger: str
    condition: str
    action: str
    severity: str
    webhook_url: Optional[str] = None
    scope_agents: List[str] = Field(default_factory=list)
    enabled: bool = True
    source: str = "yaml"  # "yaml" | "db" — surfaces where the engine loaded it from
    version: int = 1


class PolicyListResponse(BaseModel):
    policies: List[PolicyOut]
    source: str  # which store is authoritative right now
    engine_loaded: bool


class PolicyCreate(BaseModel):
    """Body for POST /v1/policies (Phase 4)."""

    name: str
    description: Optional[str] = None
    trigger: str
    condition: str
    action: str
    severity: str
    webhook_url: Optional[str] = None
    scope_agents: List[str] = Field(default_factory=list)
    enabled: bool = True


class PolicyUpdate(BaseModel):
    """Body for PUT /v1/policies/{name} (Phase 4). Every field optional;
    omitted fields keep their existing value. The engine reload is
    triggered after a successful write."""

    description: Optional[str] = None
    trigger: Optional[str] = None
    condition: Optional[str] = None
    action: Optional[str] = None
    severity: Optional[str] = None
    webhook_url: Optional[str] = None
    scope_agents: Optional[List[str]] = None
    enabled: Optional[bool] = None


class PolicyAuditEntry(BaseModel):
    """One row from the policy_audit log."""

    id: str
    policy_name: str
    action: str  # "create" | "update" | "delete"
    before: Optional[Any] = None
    after: Optional[Any] = None
    actor: Optional[str] = None
    created_at: Optional[datetime] = None


class PolicyAuditResponse(BaseModel):
    entries: List[PolicyAuditEntry]
    total: int
