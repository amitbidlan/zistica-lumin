import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends

from db import Database, get_db
from models import IngestRequest, IngestResponse, SpanInput
from routers.traces import _row_to_span, _row_to_trace
from ws import manager as ws_manager

router = APIRouter()


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


def _insert_span(db: Database, span: SpanInput) -> None:
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
            started_at, ended_at, status, error_message, tool_name, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            metadata = EXCLUDED.metadata
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
        ],
    )


def _utc_now_naive() -> datetime:
    """Naive UTC — same shape as user-provided timestamps after normalization.

    DuckDB's CURRENT_TIMESTAMP DEFAULT returns local time, which would create
    an internal inconsistency with started_at/ended_at (stored UTC). Passing
    ingest_at explicitly keeps everything in UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _upsert_trace_from_span(db: Database, span: SpanInput) -> bool:
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
            INSERT INTO traces (id, name, input, output, started_at, ended_at, ingest_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                input = EXCLUDED.input,
                output = EXCLUDED.output,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                ingest_at = EXCLUDED.ingest_at
            """,
            [trace_id, span.name, span.input, span.output, started_at, ended_at, ingest_at],
        )
    else:
        db.execute(
            """
            INSERT INTO traces (id, started_at, ingest_at)
            VALUES (?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [trace_id, started_at, ingest_at],
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


@router.post("/v1/spans", response_model=IngestResponse)
def ingest_spans(payload: IngestRequest, db: Database = Depends(get_db)) -> IngestResponse:
    accepted = 0
    for span in payload.spans:
        _insert_span(db, span)
        is_new_trace = _upsert_trace_from_span(db, span)
        _broadcast_after_insert(db, span, is_new_trace)
        accepted += 1
    return IngestResponse(accepted=accepted)
