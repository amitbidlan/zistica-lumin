"""Sessions endpoints — derived from the traces table via GROUP BY.

A session is just a value of `traces.session_id` shared across multiple
trace rows. No separate `sessions` table; aggregations are computed on
the fly. session_id is optional: traces without one don't appear here.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db import Database, get_db
from models import Session, SessionDetail
from routers.traces import _row_to_trace

router = APIRouter()


def _row_to_session(row: dict) -> Session:
    first_seen: Optional[datetime] = row.get("first_seen")
    last_seen: Optional[datetime] = row.get("last_seen")
    wall_duration_ms: Optional[int] = None
    if first_seen is not None and last_seen is not None:
        wall_duration_ms = int((last_seen - first_seen).total_seconds() * 1000)

    qs = row.get("quality_score")
    cost = row.get("total_cost_usd")
    return Session(
        session_id=row["session_id"],
        trace_count=int(row.get("trace_count") or 0),
        total_duration_ms=int(row.get("total_duration_ms") or 0),
        total_cost_usd=float(cost) if cost is not None else 0.0,
        total_tokens=int(row.get("total_tokens") or 0),
        quality_score=float(qs) if qs is not None else None,
        first_seen=first_seen,
        last_seen=last_seen,
        wall_duration_ms=wall_duration_ms,
    )


# Per-trace cost / tokens. Mirrors the GREATEST(stored, sum-from-spans)
# pattern that /v1/traces uses, so framework-only-spans flows
# (every OTel exporter does this — no separate POST /v1/traces call)
# still aggregate up correctly. Without this, sessions reported $0/0
# tokens for every span-only ingest.
_BASE_AGG = """
    session_id,
    COUNT(*) AS trace_count,
    SUM(COALESCE(DATEDIFF('millisecond', started_at, ended_at), 0)) AS total_duration_ms,
    SUM(
        GREATEST(
            COALESCE(total_cost_usd, 0),
            COALESCE(
                (SELECT SUM(cost_usd) FROM spans WHERE trace_id = traces.id),
                0
            )
        )
    ) AS total_cost_usd,
    SUM(
        GREATEST(
            COALESCE(total_tokens, 0),
            COALESCE(
                (SELECT SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0))
                 FROM spans WHERE trace_id = traces.id),
                0
            )
        )
    ) AS total_tokens,
    AVG(quality_score) AS quality_score,
    MIN(started_at) AS first_seen,
    MAX(COALESCE(ended_at, started_at)) AS last_seen
"""


@router.get("/v1/sessions", response_model=List[Session])
def list_sessions(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    project: Optional[str] = Query(
        None,
        description="Multi-tenant scope. Same semantics as /v1/traces.",
    ),
    db: Database = Depends(get_db),
) -> List[Session]:
    extra: list = ["session_id IS NOT NULL", "session_id != ''"]
    params: list = []
    if project:
        if project == "default":
            extra.append("COALESCE(project, '') IN ('', 'default')")
        else:
            extra.append("project = ?")
            params.append(project)
    where_clause = "WHERE " + " AND ".join(extra)
    rows = db.fetchall_dict(
        f"""
        SELECT {_BASE_AGG}
        FROM traces
        {where_clause}
        GROUP BY session_id
        ORDER BY last_seen DESC NULLS LAST
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    return [_row_to_session(r) for r in rows]


@router.get("/v1/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: str, db: Database = Depends(get_db)) -> SessionDetail:
    summary_row = db.fetchone_dict(
        f"""
        SELECT {_BASE_AGG}
        FROM traces
        WHERE session_id = ?
        GROUP BY session_id
        """,
        [session_id],
    )
    if summary_row is None:
        raise HTTPException(status_code=404, detail="session not found")

    trace_rows = db.fetchall_dict(
        """
        SELECT * FROM traces
        WHERE session_id = ?
        ORDER BY started_at ASC
        """,
        [session_id],
    )
    summary = _row_to_session(summary_row)
    return SessionDetail(
        **summary.model_dump(),
        traces=[_row_to_trace(r) for r in trace_rows],
    )
