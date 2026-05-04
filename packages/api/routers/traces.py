import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db import Database, get_db
from models import Span, Trace, TraceCreate

router = APIRouter()


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
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


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row_to_trace(row: dict) -> Trace:
    started = row.get("started_at")
    ended = row.get("ended_at")
    duration_ms = None
    if started is not None and ended is not None:
        duration_ms = int((ended - started).total_seconds() * 1000)

    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            pass

    cost = row.get("total_cost_usd")
    return Trace(
        id=row["id"],
        name=row.get("name"),
        input=row.get("input"),
        output=row.get("output"),
        started_at=started,
        ended_at=ended,
        duration_ms=duration_ms,
        total_tokens=row.get("total_tokens") or 0,
        total_cost_usd=float(cost) if cost is not None else 0.0,
        quality_score=row.get("quality_score"),
        user_id=row.get("user_id") or "",
        session_id=row.get("session_id"),
        tags=row.get("tags"),
        metadata=metadata,
        ingest_at=row.get("ingest_at"),
    )


# SQL fragment that aggregates cost + tokens from a trace's spans
# and returns the larger of the user-supplied value (set via POST
# /v1/traces) or the computed sum. Lets the trace list and detail
# show meaningful totals even when only spans were ingested.
_TRACE_WITH_AGGREGATES = """
SELECT
    t.*,
    GREATEST(
        COALESCE(t.total_cost_usd, 0),
        COALESCE((SELECT SUM(cost_usd) FROM spans WHERE trace_id = t.id), 0)
    ) AS total_cost_usd,
    GREATEST(
        COALESCE(t.total_tokens, 0),
        COALESCE((
            SELECT SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0))
            FROM spans WHERE trace_id = t.id
        ), 0)
    ) AS total_tokens
FROM traces t
"""


def _row_to_span(row: dict) -> Span:
    started = row.get("started_at")
    ended = row.get("ended_at")
    duration_ms = None
    if started is not None and ended is not None:
        duration_ms = int((ended - started).total_seconds() * 1000)

    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            pass

    cost = row.get("cost_usd")
    return Span(
        id=row["id"],
        trace_id=row["trace_id"],
        parent_span_id=row.get("parent_span_id"),
        type=row.get("type"),
        name=row.get("name"),
        input=row.get("input"),
        output=row.get("output"),
        model=row.get("model"),
        provider=row.get("provider"),
        tokens_input=row.get("tokens_input"),
        tokens_output=row.get("tokens_output"),
        cost_usd=float(cost) if cost is not None else None,
        started_at=started,
        ended_at=ended,
        duration_ms=duration_ms,
        status=row.get("status") or "ok",
        error_message=row.get("error_message"),
        tool_name=row.get("tool_name"),
        metadata=metadata,
        span_subtype=row.get("span_subtype"),
        thinking_tokens=row.get("thinking_tokens"),
        session_id=row.get("session_id"),
    )


@router.post("/v1/traces", response_model=Trace)
def upsert_trace(payload: TraceCreate, db: Database = Depends(get_db)) -> Trace:
    started_at = _parse_ts(payload.started_at)
    ended_at = _parse_ts(payload.ended_at)
    metadata_str = json.dumps(payload.metadata) if payload.metadata is not None else None

    db.execute(
        """
        INSERT INTO traces (
            id, name, input, output, started_at, ended_at,
            total_tokens, total_cost_usd, quality_score,
            user_id, session_id, tags, metadata, ingest_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            input = EXCLUDED.input,
            output = EXCLUDED.output,
            started_at = EXCLUDED.started_at,
            ended_at = EXCLUDED.ended_at,
            total_tokens = EXCLUDED.total_tokens,
            total_cost_usd = EXCLUDED.total_cost_usd,
            quality_score = EXCLUDED.quality_score,
            user_id = EXCLUDED.user_id,
            session_id = EXCLUDED.session_id,
            tags = EXCLUDED.tags,
            metadata = EXCLUDED.metadata,
            ingest_at = EXCLUDED.ingest_at
        """,
        [
            payload.id,
            payload.name,
            payload.input,
            payload.output,
            started_at,
            ended_at,
            payload.total_tokens or 0,
            payload.total_cost_usd or 0.0,
            payload.quality_score,
            payload.user_id or "",
            payload.session_id,
            payload.tags,
            metadata_str,
            _utc_now_naive(),
        ],
    )

    row = db.fetchone_dict("SELECT * FROM traces WHERE id = ?", [payload.id])
    if row is None:
        raise HTTPException(500, "trace upsert failed")
    return _row_to_trace(row)


@router.get("/v1/traces", response_model=List[Trace])
def list_traces(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> List[Trace]:
    rows = db.fetchall_dict(
        _TRACE_WITH_AGGREGATES + " ORDER BY t.started_at DESC LIMIT ? OFFSET ?",
        [limit, offset],
    )
    return [_row_to_trace(r) for r in rows]


@router.get("/v1/traces/{trace_id}", response_model=Trace)
def get_trace(trace_id: str, db: Database = Depends(get_db)) -> Trace:
    row = db.fetchone_dict(
        _TRACE_WITH_AGGREGATES + " WHERE t.id = ?", [trace_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return _row_to_trace(row)


@router.get("/v1/traces/{trace_id}/spans", response_model=List[Span])
def get_trace_spans(trace_id: str, db: Database = Depends(get_db)) -> List[Span]:
    rows = db.fetchall_dict(
        """
        SELECT * FROM spans
        WHERE trace_id = ?
        ORDER BY started_at ASC
        """,
        [trace_id],
    )

    # session_id inheritance: when the SDK or framework emits child spans
    # without a session_id (because the value is only set on the OTel
    # resource of the *root span* attrs), every child should still appear
    # in the right /sessions group. Inherit from the trace row.
    #
    # Done at read time rather than ingest time to avoid an extra query
    # in the hot path, and because orphan-child ordering means the trace
    # row may not yet know its session_id at insert time.
    trace_row = db.fetchone_dict(
        "SELECT session_id FROM traces WHERE id = ?", [trace_id]
    )
    trace_sid = trace_row.get("session_id") if trace_row else None
    if trace_sid:
        for r in rows:
            if r.get("session_id") is None:
                r["session_id"] = trace_sid

    return [_row_to_span(r) for r in rows]
