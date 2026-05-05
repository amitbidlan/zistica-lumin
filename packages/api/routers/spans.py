import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header

from db import Database, get_db
from models import IngestRequest, IngestResponse, SpanInput
import policy_runtime
from routers.traces import _row_to_span, _row_to_trace
from ws import manager as ws_manager

router = APIRouter()


# The framework dimension on the agent grid is intentionally a closed
# set. Each allowed value corresponds to a published Lumin SDK package
# (or the default Python SDK). Anything else (a typo'd header, an
# operator's ad-hoc test value like "live_demo") would otherwise show
# up as a top-level "framework" section in the dashboard, which makes
# the headline list unstable. Normalize to "default" so unknown values
# silently fall under the Python SDK bucket — operators that want a
# dedicated section should ship a package.
ALLOWED_PROJECTS = frozenset({"openclaw", "mastra", "voltagent", "default"})


def _normalize_project(raw: Optional[str]) -> Optional[str]:
    """Coerce ``X-Lumin-Project`` to one of the four allowed values
    (or None when no header was sent).

    None / empty header → None — caller persists NULL, downstream
    coalesces to "default" at read time. We don't fabricate "default"
    on ingest because that loses the "header was unset" signal that
    the metrics view sometimes wants.

    Unknown value (case-insensitive after strip) → "default". This is
    what folds ``live_demo`` and any future free-form mislabel under
    the Python SDK headline.
    """
    if not raw:
        return None
    v = raw.strip().lower()
    if not v:
        return None
    if v in ALLOWED_PROJECTS:
        return v
    return "default"


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp and return a naive UTC datetime.

    DuckDB TIMESTAMP columns are timezone-naive; passing a tz-aware datetime
    causes a local-time shift on storage. Normalize to naive UTC so values
    round-trip correctly regardless of the server's local timezone.
    """
    if ts is None:
        return None
    s = ts
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _resolve_status_and_error(span: SpanInput) -> tuple[str, Optional[str]]:
    error_message = span.error_message or span.error
    if span.status:
        status = span.status
    elif error_message:
        status = "error"
    else:
        status = "ok"
    return status, error_message


def _insert_span(db: Database, span: SpanInput, project: Optional[str] = None) -> None:
    trace_id = span.trace_id or span.id
    status, error_message = _resolve_status_and_error(span)
    started_at = _parse_ts(span.started_at)
    ended_at = _parse_ts(span.ended_at)
    metadata_str = json.dumps(span.metadata) if span.metadata is not None else None

    db.execute(
        """
        INSERT INTO spans (
            id, trace_id, parent_span_id, type, name, input, output,
            model, provider, tokens_input, tokens_output, cost_usd,
            started_at, ended_at, status, error_message, tool_name, metadata,
            span_subtype, thinking_tokens, session_id, project
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            trace_id = EXCLUDED.trace_id,
            parent_span_id = EXCLUDED.parent_span_id,
            type = EXCLUDED.type,
            name = EXCLUDED.name,
            input = EXCLUDED.input,
            output = EXCLUDED.output,
            model = EXCLUDED.model,
            provider = EXCLUDED.provider,
            tokens_input = EXCLUDED.tokens_input,
            tokens_output = EXCLUDED.tokens_output,
            cost_usd = EXCLUDED.cost_usd,
            started_at = EXCLUDED.started_at,
            ended_at = EXCLUDED.ended_at,
            status = EXCLUDED.status,
            error_message = EXCLUDED.error_message,
            tool_name = EXCLUDED.tool_name,
            metadata = EXCLUDED.metadata,
            span_subtype = EXCLUDED.span_subtype,
            thinking_tokens = EXCLUDED.thinking_tokens,
            session_id = EXCLUDED.session_id,
            project = COALESCE(EXCLUDED.project, spans.project)
        """,
        [
            span.id,
            trace_id,
            span.parent_span_id,
            span.type or "custom",
            span.name,
            span.input,
            span.output,
            span.model,
            span.provider,
            span.tokens_input,
            span.tokens_output,
            span.cost_usd,
            started_at,
            ended_at,
            status,
            error_message,
            span.tool_name,
            metadata_str,
            span.span_subtype,
            span.thinking_tokens,
            span.session_id,
            project,
        ],
    )


def _utc_now_naive() -> datetime:
    """Naive UTC — same shape as user-provided timestamps after normalization.

    DuckDB's CURRENT_TIMESTAMP DEFAULT returns local time, which would create
    an internal inconsistency with started_at/ended_at (stored UTC). Passing
    ingest_at explicitly keeps everything in UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _upsert_trace_from_span(db: Database, span: SpanInput, project: Optional[str] = None) -> bool:
    """Auto-create or update the parent trace based on the span.

    - Root spans (no parent) populate the trace fully.
    - Non-root spans only insert a stub if the trace doesn't exist yet.

    Returns True if a brand-new trace row was created (so callers can
    broadcast a ``new_trace`` event), False if an existing trace was
    updated (or left untouched).
    """
    trace_id = span.trace_id or span.id
    started_at = _parse_ts(span.started_at)
    ended_at = _parse_ts(span.ended_at)
    ingest_at = _utc_now_naive()

    pre_existing = db.fetchone("SELECT 1 FROM traces WHERE id = ?", [trace_id])
    is_new_trace = pre_existing is None

    if span.parent_span_id is None:
        db.execute(
            """
            INSERT INTO traces (
                id, name, input, output, started_at, ended_at, session_id,
                ingest_at, project
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                input = EXCLUDED.input,
                output = EXCLUDED.output,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                session_id = COALESCE(EXCLUDED.session_id, traces.session_id),
                ingest_at = EXCLUDED.ingest_at,
                project = COALESCE(EXCLUDED.project, traces.project)
            """,
            [
                trace_id, span.name, span.input, span.output,
                started_at, ended_at, span.session_id, ingest_at, project,
            ],
        )
    else:
        # Stub upsert from a non-root span. Don't overwrite the trace if it
        # exists — but DO populate session_id + project on the stub so
        # the orphan trace shows up in its session/framework right away
        # (the eventual root span will COALESCE the same values).
        db.execute(
            """
            INSERT INTO traces (id, started_at, session_id, ingest_at, project)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                session_id = COALESCE(traces.session_id, EXCLUDED.session_id),
                project = COALESCE(traces.project, EXCLUDED.project)
            """,
            [trace_id, started_at, span.session_id, ingest_at, project],
        )

    return is_new_trace


def _broadcast_after_insert(db: Database, span: SpanInput, is_new_trace: bool) -> None:
    """Best-effort: emit ``new_span`` (always) and ``new_trace`` whenever
    the trace's user-visible state meaningfully changes.

    "Meaningfully changes" means either:
      - The trace row was just created (e.g. an orphan child span made a
        stub), so the dashboard hasn't seen this trace_id yet; OR
      - A root span (parent_span_id is None) arrived. Even if the trace
        already existed as a stub, the root populates name/input/output/
        ended_at — the dashboard needs to update its cached row.

    Skipping the second case (which an earlier version did) left the
    dashboard showing a permanent stub when an orphan child landed
    before its root.

    Failures are swallowed — broadcast must never affect ingest.
    """
    try:
        trace_id = span.trace_id or span.id

        span_row = db.fetchone_dict("SELECT * FROM spans WHERE id = ?", [span.id])
        if span_row is not None:
            ws_manager.broadcast_threadsafe(
                {
                    "type": "new_span",
                    "trace_id": trace_id,
                    "span": _row_to_span(span_row).model_dump(mode="json"),
                }
            )

        is_root = span.parent_span_id is None
        if is_new_trace or is_root:
            trace_row = db.fetchone_dict(
                "SELECT * FROM traces WHERE id = ?", [trace_id]
            )
            if trace_row is not None:
                ws_manager.broadcast_threadsafe(
                    {
                        "type": "new_trace",
                        "trace": _row_to_trace(trace_row).model_dump(mode="json"),
                    }
                )
    except Exception:
        # Swallow — broadcast is best-effort. Rule 7 generalized: ingest
        # must never break because of an observability subsystem.
        pass


def _evaluate_policies_for_batch(
    db: Database, spans: List[SpanInput]
) -> None:
    """Run span_end + trace_end policies for an ingested batch.

    Called from FastAPI BackgroundTasks AFTER the HTTP response is
    returned, so callers don't pay eval latency. Idempotency at the
    DB level (deterministic violation id + ON CONFLICT DO NOTHING)
    means we can re-evaluate aggressively without creating duplicates.

    For trace_end: we re-evaluate on EVERY span ingest, not just on
    root-span arrival. With OTel BatchSpanProcessor, the root often
    flushes before the last child — a "wait for root" heuristic
    would miss late children. Re-evaluating on every span ingest
    catches them; the deterministic id + ON CONFLICT means the
    extra evals don't pollute the table.

    Per Rule 7 every step swallows exceptions — a broken policy or
    DB hiccup must not cascade back to the agent (the response was
    already returned, but we still keep the failure local).
    """
    try:
        for span in spans:
            policy_runtime.evaluate_span(db, span)
        # De-dup unique trace_ids touched by this batch — re-evaluate
        # trace_end for each. Late-arriving children for an existing
        # trace will trigger re-eval and flip a previously-passing
        # condition on if they crossed it.
        trace_ids = {(s.trace_id or s.id) for s in spans}
        for tid in trace_ids:
            policy_runtime.evaluate_trace(db, tid)
    except Exception:
        # Already inside a background task — no caller to surface to.
        # Logged via logger inside policy_runtime; silent here.
        pass


@router.post("/v1/spans", response_model=IngestResponse)
def ingest_spans(
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    x_lumin_project: Optional[str] = Header(default=None),
) -> IngestResponse:
    """Ingest a batch of spans.

    The optional ``X-Lumin-Project`` header lets the integration declare
    which framework it is (set by every TS exporter — openclaw / mastra
    / voltagent — and the Python SDK config). We persist it on each
    span + propagate to the trace so the agent grid can group by
    framework. Empty / missing header → project stays NULL, treated
    as "default" downstream.
    """
    project = _normalize_project(x_lumin_project)

    # Validate every span's started_at BEFORE writing any of them.
    # The traces.started_at column is NOT NULL — feeding it None from
    # an unparseable timestamp blew up at INSERT time as a 500. Catch
    # it here and return a clean 400 so the integration sees the
    # actual problem instead of "internal server error".
    from fastapi import HTTPException
    for span in payload.spans:
        if _parse_ts(span.started_at) is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"span {span.id!r}: started_at must be an ISO 8601 "
                    f"timestamp (got {span.started_at!r})"
                ),
            )
    accepted = 0
    for span in payload.spans:
        _insert_span(db, span, project=project)
        is_new_trace = _upsert_trace_from_span(db, span, project=project)
        _broadcast_after_insert(db, span, is_new_trace)
        accepted += 1

    # Move policy evaluation off the synchronous request path. With
    # FastAPI BackgroundTasks the response returns immediately and
    # the eval runs after — no agent ever waits on policy work. The
    # background task is queued onto the same threadpool but doesn't
    # delay this response.
    if payload.spans:
        background_tasks.add_task(
            _evaluate_policies_for_batch, db, list(payload.spans)
        )

    return IngestResponse(accepted=accepted)
