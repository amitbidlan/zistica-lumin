"""Agent-first observability — Phase 1 of the agent-card dashboard.

Treats the trace's `name` as the agent identity. Every distinct name
becomes an entry in the agent grid; the dashboard groups, ranks, and
links into the underlying traces.

This is intentionally a thin layer over the existing traces table —
no new schema, no new ingest path, no new DB writes. We surface
agents by querying what's already there. Future phases can promote
agent identity to a first-class column once the UX is validated.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from db import Database, get_db
from models import Trace
from routers.traces import _row_to_trace

router = APIRouter()


# --- response models -------------------------------------------------------


class AgentSummary(BaseModel):
    """One card on the agent grid."""

    name: str
    # The integration / framework that emitted these traces. Comes
    # from the X-Lumin-Project header at ingest. Common values:
    # "openclaw" / "mastra" / "voltagent" / "default" (Python SDK).
    # Used by the dashboard to group cards into framework sections.
    project: str
    trace_count: int
    total_cost_usd: float
    total_tokens: int
    avg_duration_ms: int
    error_rate: float          # 0.0 – 1.0, fraction of traces with any error span
    violation_count: int
    has_violations: bool
    last_seen: Optional[datetime]
    top_model: Optional[str]
    top_provider: Optional[str]
    # All distinct LLM providers used by this agent's traces. Lets
    # the dashboard show "uses anthropic + openai" badges + filter
    # by provider.
    providers: List[str] = []
    # Live indicator — shipped in Phase 1 so operators see "did this
    # agent do anything recently?" without having to drill in.
    seconds_since_last_seen: Optional[int]
    activity: str  # "active" / "idle" / "dormant"
    # Phase 2 additions —
    # `active_traces`: count of in-flight traces (started_at set,
    #     ended_at NULL, started recently — old orphans excluded).
    #     The dashboard renders this as a "N in flight" pill that
    #     pulses on the card while the agent is actually thinking.
    # `activity_buckets`: 12 ints, each = trace count in a 5-minute
    #     bucket over the last 60 min. Index 0 = most recent (0-5min
    #     ago); index 11 = oldest (55-60min ago). Drawn as a
    #     sparkline so operators can spot bursty vs steady agents
    #     without opening the detail page.
    active_traces: int = 0
    activity_buckets: List[int] = []


class AgentListResponse(BaseModel):
    agents: List[AgentSummary]
    window_hours: int
    # Set of distinct projects in the response — the dashboard uses
    # this to render section headers without re-aggregating.
    projects: List[str] = []
    # `older_data_exists`: True when the DB has traces older than the
    # current window. Lets the empty-state on /agents say "0 in last
    # 24h, but data exists — try the 7d filter" instead of just
    # "no agents". Prevents the confusing case where /traces shows
    # rows but /agents shows nothing.
    older_data_exists: bool = False


class AgentDetail(AgentSummary):
    """Detail page payload — same metrics + recent traces + violation
    breakdown by policy."""

    recent_traces: List[Trace] = []
    violations_by_policy: dict = {}
    violations_by_severity: dict = {}


# --- helpers ---------------------------------------------------------------


def _activity_label(seconds: Optional[int]) -> str:
    """Three-tier activity bucket. Conservative — bursty agents that
    chat-then-pause won't flicker between active/dormant on each turn."""
    if seconds is None:
        return "dormant"
    if seconds < 30:
        return "active"
    if seconds < 300:
        return "idle"
    return "dormant"


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- endpoints -------------------------------------------------------------


@router.get("/v1/agents", response_model=AgentListResponse)
def list_agents(
    window_hours: int = Query(24, ge=1, le=24 * 30,
                              description="Activity window for aggregated metrics"),
    search: Optional[str] = Query(None, description="Substring filter on agent name"),
    project: Optional[str] = Query(None, description="Filter by framework: openclaw / mastra / voltagent / default"),
    provider: Optional[str] = Query(None, description="Filter by LLM provider: anthropic / openai / ollama / etc."),
    limit: int = Query(100, ge=1, le=500),
    db: Database = Depends(get_db),
) -> AgentListResponse:
    """List all distinct agents (= distinct trace.name) and their
    activity metrics over the configured window.

    The query is one DuckDB aggregate — no per-agent N+1. Joins against
    spans for cost / token / model / error_rate, against
    policy_violations for the violation count.
    """
    cutoff = _utc_now_naive() - timedelta(hours=window_hours)

    # Step 1: aggregate by (name, project) from traces + spans +
    # violations. Two agents with the same name from different
    # frameworks (rare but possible) stay separate cards, which is
    # the right call — they came from different code, treat them
    # independently.
    rows = db.fetchall_dict(
        """
        WITH trace_agg AS (
          SELECT
            t.id AS trace_id,
            t.name AS name,
            COALESCE(NULLIF(t.project, ''), 'default') AS project,
            t.started_at,
            t.ingest_at,
            DATEDIFF('millisecond', t.started_at, t.ended_at) AS duration_ms,
            COALESCE(
              (SELECT SUM(cost_usd) FROM spans WHERE trace_id = t.id), 0
            ) AS span_cost_usd,
            COALESCE(
              (SELECT SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0))
               FROM spans WHERE trace_id = t.id),
              0
            ) AS span_tokens,
            COALESCE(
              (SELECT COUNT(*) FROM spans WHERE trace_id = t.id AND status = 'error'), 0
            ) AS span_errors,
            COALESCE(
              (SELECT COUNT(*) FROM policy_violations WHERE trace_id = t.id), 0
            ) AS violation_count
          FROM traces t
          WHERE t.name IS NOT NULL
            AND t.name != ''
            AND t.started_at >= ?
        )
        SELECT
          name,
          project,
          COUNT(*) AS trace_count,
          SUM(GREATEST(span_cost_usd, 0)) AS total_cost_usd,
          SUM(span_tokens) AS total_tokens,
          AVG(NULLIF(duration_ms, 0)) AS avg_duration_ms,
          MAX(ingest_at) AS last_seen,
          (CAST(SUM(CASE WHEN span_errors > 0 THEN 1 ELSE 0 END) AS DOUBLE)
             / GREATEST(COUNT(*), 1)) AS error_rate,
          SUM(violation_count) AS violation_count
        FROM trace_agg
        GROUP BY name, project
        """,
        [cutoff],
    )

    # Active-traces count + 12-bucket activity sparkline.
    # Active = started recently (last 10 min) AND ended_at not yet
    # set. Older "active" rows are almost always orphan stubs whose
    # root span never arrived — excluding them keeps the "thinking
    # now" indicator honest.
    active_cutoff = _utc_now_naive() - timedelta(minutes=10)
    active_rows = db.fetchall_dict(
        """
        SELECT
          name,
          COALESCE(NULLIF(project, ''), 'default') AS agent_project,
          COUNT(*) AS active
        FROM traces
        WHERE ended_at IS NULL
          AND started_at >= ?
          AND name IS NOT NULL
          AND name != ''
        GROUP BY name, agent_project
        """,
        [active_cutoff],
    )
    active_by_agent = {(r["name"], r["agent_project"]): int(r["active"])
                       for r in active_rows}

    # Sparkline: 12 buckets × 5 min. bucket 0 = most recent.
    spark_cutoff = _utc_now_naive() - timedelta(minutes=60)
    spark_rows = db.fetchall_dict(
        """
        SELECT
          name,
          COALESCE(NULLIF(project, ''), 'default') AS agent_project,
          CAST(DATE_DIFF('minute', started_at, ?) / 5 AS INTEGER) AS bucket,
          COUNT(*) AS n
        FROM traces
        WHERE started_at >= ?
          AND name IS NOT NULL
          AND name != ''
        GROUP BY name, agent_project, bucket
        """,
        [_utc_now_naive(), spark_cutoff],
    )
    buckets_by_agent: dict = {}
    for r in spark_rows:
        key = (r["name"], r["agent_project"])
        idx = int(r["bucket"])
        if 0 <= idx < 12:
            arr = buckets_by_agent.setdefault(key, [0] * 12)
            arr[idx] += int(r["n"])

    # Step 2: per-(agent, project) top model + full provider list.
    # Uses an alias for the COALESCEd project name so we can also
    # GROUP BY it without ambiguity (both traces and spans now
    # have a project column).
    model_rows = db.fetchall_dict(
        """
        SELECT
          t.name AS name,
          COALESCE(NULLIF(t.project, ''), 'default') AS agent_project,
          s.model AS model,
          s.provider AS provider,
          COUNT(*) AS uses
        FROM traces t
        JOIN spans s ON s.trace_id = t.id
        WHERE t.name IS NOT NULL
          AND s.model IS NOT NULL
          AND t.started_at >= ?
        GROUP BY t.name, agent_project, s.model, s.provider
        """,
        [cutoff],
    )
    # Index by (name, project) so two integrations using the same
    # agent name don't bleed into each other's stats
    top_by_agent: dict = {}
    providers_by_agent: dict = {}
    for r in model_rows:
        key = (r["name"], r["agent_project"])
        cur = top_by_agent.get(key)
        if cur is None or r["uses"] > cur["uses"]:
            top_by_agent[key] = r
        if r.get("provider"):
            providers_by_agent.setdefault(key, set()).add(r["provider"])

    # Step 3: stitch + filter + paginate
    now_naive = _utc_now_naive()
    agents: List[AgentSummary] = []
    for r in rows:
        name = r["name"]
        proj = r.get("project") or "default"
        key = (name, proj)
        if search and search.lower() not in name.lower():
            continue
        if project and proj != project:
            continue
        agent_providers = sorted(providers_by_agent.get(key, set()))
        if provider and provider not in agent_providers:
            continue
        last_seen = r.get("last_seen")
        seconds = None
        if last_seen is not None:
            seconds = int((now_naive - last_seen).total_seconds())
        top = top_by_agent.get(key) or {}
        agents.append(AgentSummary(
            name=name,
            project=proj,
            trace_count=int(r.get("trace_count") or 0),
            total_cost_usd=float(r.get("total_cost_usd") or 0.0),
            total_tokens=int(r.get("total_tokens") or 0),
            avg_duration_ms=int(r.get("avg_duration_ms") or 0),
            error_rate=float(r.get("error_rate") or 0.0),
            violation_count=int(r.get("violation_count") or 0),
            has_violations=int(r.get("violation_count") or 0) > 0,
            last_seen=last_seen,
            top_model=top.get("model"),
            top_provider=top.get("provider"),
            providers=agent_providers,
            seconds_since_last_seen=seconds,
            activity=_activity_label(seconds),
            active_traces=active_by_agent.get(key, 0),
            activity_buckets=buckets_by_agent.get(key, [0] * 12),
        ))

    # Order most-recently-active first (smaller seconds = more recent)
    agents.sort(key=lambda a: (a.seconds_since_last_seen
                               if a.seconds_since_last_seen is not None
                               else 10**12))

    distinct_projects = sorted({a.project for a in agents})

    # Quick check — does the DB have traces OUTSIDE the current window
    # whose name is set? If so, the empty-state on the dashboard can
    # surface a "try a longer window" hint instead of just "no agents".
    # Cheap query: scoped by `started_at < cutoff`, indexed primary
    # key, returns the moment one row matches.
    older_row = db.fetchone(
        "SELECT 1 FROM traces WHERE started_at < ? AND name IS NOT NULL "
        "AND name <> '' LIMIT 1",
        [cutoff],
    )
    older_data_exists = older_row is not None

    return AgentListResponse(
        agents=agents[:limit],
        window_hours=window_hours,
        projects=distinct_projects,
        older_data_exists=older_data_exists,
    )


@router.get("/v1/agents/{agent_name:path}", response_model=AgentDetail)
def get_agent(
    agent_name: str,
    window_hours: int = Query(24, ge=1, le=24 * 30),
    db: Database = Depends(get_db),
) -> AgentDetail:
    """One agent's detail page: same metrics as the card, plus the
    most-recent traces and a per-policy violation breakdown so the
    operator can scan what's been firing."""
    cutoff = _utc_now_naive() - timedelta(hours=window_hours)

    # Use the list endpoint's aggregator to build the summary — keeps
    # the math identical between grid + detail.
    summary_rows = db.fetchall_dict(
        """
        SELECT
          t.id AS trace_id,
          t.started_at,
          t.ingest_at,
          DATEDIFF('millisecond', t.started_at, t.ended_at) AS duration_ms,
          COALESCE(
            (SELECT SUM(cost_usd) FROM spans WHERE trace_id = t.id), 0
          ) AS span_cost_usd,
          COALESCE(
            (SELECT SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0))
             FROM spans WHERE trace_id = t.id),
            0
          ) AS span_tokens,
          COALESCE(
            (SELECT COUNT(*) FROM spans WHERE trace_id = t.id AND status = 'error'), 0
          ) AS span_errors,
          COALESCE(
            (SELECT COUNT(*) FROM policy_violations WHERE trace_id = t.id), 0
          ) AS violation_count
        FROM traces t
        WHERE t.name = ?
          AND t.started_at >= ?
        """,
        [agent_name, cutoff],
    )
    if not summary_rows:
        raise HTTPException(404, detail=f"agent {agent_name!r} has no traces in the window")

    trace_count = len(summary_rows)
    total_cost = sum(max(float(r.get("span_cost_usd") or 0), 0) for r in summary_rows)
    total_tokens = sum(int(r.get("span_tokens") or 0) for r in summary_rows)
    durations = [int(r["duration_ms"]) for r in summary_rows if r.get("duration_ms")]
    avg_duration_ms = int(sum(durations) / len(durations)) if durations else 0
    error_traces = sum(1 for r in summary_rows if int(r.get("span_errors") or 0) > 0)
    error_rate = error_traces / trace_count if trace_count else 0.0
    violation_count = sum(int(r.get("violation_count") or 0) for r in summary_rows)
    last_seen_values = [r["ingest_at"] for r in summary_rows if r.get("ingest_at")]
    last_seen = max(last_seen_values) if last_seen_values else None

    seconds = None
    if last_seen is not None:
        seconds = int((_utc_now_naive() - last_seen).total_seconds())

    # Top model / provider + full provider list for this agent
    model_rows = db.fetchall_dict(
        """
        SELECT s.model AS model, s.provider AS provider, COUNT(*) AS uses
        FROM traces t
        JOIN spans s ON s.trace_id = t.id
        WHERE t.name = ?
          AND s.model IS NOT NULL
          AND t.started_at >= ?
        GROUP BY s.model, s.provider
        ORDER BY uses DESC
        """,
        [agent_name, cutoff],
    )
    top_model = model_rows[0]["model"] if model_rows else None
    top_provider = model_rows[0]["provider"] if model_rows else None
    providers = sorted({r["provider"] for r in model_rows if r.get("provider")})

    # Project (mode of project across this agent's traces; in practice
    # all traces of one agent share a project, so this picks the right
    # value).
    project_row = db.fetchone(
        """
        SELECT COALESCE(NULLIF(project, ''), 'default') AS p, COUNT(*) AS n
        FROM traces
        WHERE name = ? AND started_at >= ?
        GROUP BY 1 ORDER BY n DESC LIMIT 1
        """,
        [agent_name, cutoff],
    )
    project = project_row[0] if project_row else "default"

    # Recent traces — top 20 by ingest_at desc
    recent_rows = db.fetchall_dict(
        """
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
          ) AS total_tokens,
          COALESCE(
            (SELECT COUNT(*) FROM policy_violations WHERE trace_id = t.id),
            0
          ) AS violation_count
        FROM traces t
        WHERE t.name = ?
          AND t.started_at >= ?
        ORDER BY t.ingest_at DESC
        LIMIT 20
        """,
        [agent_name, cutoff],
    )
    recent = [_row_to_trace(r) for r in recent_rows]

    # Violation breakdown by policy_name + severity
    by_policy = dict(db.fetchall(
        """
        SELECT pv.policy_name, COUNT(*)
        FROM policy_violations pv
        JOIN traces t ON pv.trace_id = t.id
        WHERE t.name = ?
          AND t.started_at >= ?
        GROUP BY pv.policy_name
        """,
        [agent_name, cutoff],
    ))
    by_severity = dict(db.fetchall(
        """
        SELECT pv.severity, COUNT(*)
        FROM policy_violations pv
        JOIN traces t ON pv.trace_id = t.id
        WHERE t.name = ?
          AND t.started_at >= ?
        GROUP BY pv.severity
        """,
        [agent_name, cutoff],
    ))

    # Phase 2 — active traces + sparkline buckets, same shape as the
    # list endpoint so the detail page can reuse the same renderer.
    active_cutoff = _utc_now_naive() - timedelta(minutes=10)
    active_row = db.fetchone(
        """
        SELECT COUNT(*) FROM traces
        WHERE name = ?
          AND ended_at IS NULL
          AND started_at >= ?
        """,
        [agent_name, active_cutoff],
    )
    active_traces = int(active_row[0]) if active_row else 0

    spark_cutoff = _utc_now_naive() - timedelta(minutes=60)
    spark_rows = db.fetchall_dict(
        """
        SELECT
          CAST(DATE_DIFF('minute', started_at, ?) / 5 AS INTEGER) AS bucket,
          COUNT(*) AS n
        FROM traces
        WHERE name = ? AND started_at >= ?
        GROUP BY bucket
        """,
        [_utc_now_naive(), agent_name, spark_cutoff],
    )
    activity_buckets = [0] * 12
    for r in spark_rows:
        idx = int(r["bucket"])
        if 0 <= idx < 12:
            activity_buckets[idx] += int(r["n"])

    return AgentDetail(
        name=agent_name,
        project=project,
        trace_count=trace_count,
        total_cost_usd=total_cost,
        total_tokens=total_tokens,
        avg_duration_ms=avg_duration_ms,
        error_rate=error_rate,
        violation_count=violation_count,
        has_violations=violation_count > 0,
        last_seen=last_seen,
        top_model=top_model,
        top_provider=top_provider,
        providers=providers,
        seconds_since_last_seen=seconds,
        activity=_activity_label(seconds),
        active_traces=active_traces,
        activity_buckets=activity_buckets,
        recent_traces=recent,
        violations_by_policy={k: int(v) for k, v in by_policy.items()},
        violations_by_severity={k: int(v) for k, v in by_severity.items()},
    )
