"""Policy violations API — Accountability Layer Part B.

The SDK evaluates policy YAML conditions in-process and POSTs any
fired violations here. The dashboard reads them back to render red
badges on traces and a Violations page.

Endpoints:
    POST /v1/violations           ingest from SDK
    GET  /v1/violations           list with filters
    GET  /v1/violations/stats     aggregates by severity / policy

The /v1/traces/{id} endpoint is also extended (in routers/traces.py)
to embed a compact list of policy_violations + has_violations bool.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import policy_metrics
import policy_runtime
import policy_store
from db import Database, get_db
from lumin.policy import (
    VALID_ACTIONS,
    VALID_SEVERITIES,
    VALID_TRIGGERS,
    Policy,
    PolicyConfigError,
)
from models import (
    PolicyAuditEntry,
    PolicyAuditResponse,
    PolicyCreate,
    PolicyListResponse,
    PolicyOut,
    PolicyUpdate,
    PolicyViolation,
    PolicyViolationsIngest,
    PolicyViolationsIngestResponse,
    PolicyViolationStats,
    PolicyViolationsResponse,
)
from policy_runtime import _violation_id, _webhook_url_safe


router = APIRouter()


def _row_to_violation(row: dict) -> PolicyViolation:
    return PolicyViolation(
        id=str(row["id"]),
        policy_name=row.get("policy_name") or "",
        policy_description=row.get("policy_description"),
        severity=row.get("severity") or "low",
        trace_id=row.get("trace_id") or "",
        span_id=row.get("span_id"),
        condition_text=row.get("condition_text"),
        action_taken=row.get("action_taken"),
        actual_value=row.get("actual_value"),
        webhook_fired=bool(row.get("webhook_fired") or False),
        webhook_url=row.get("webhook_url"),
        created_at=row.get("created_at"),
    )


@router.post("/v1/violations", response_model=PolicyViolationsIngestResponse)
def ingest_violations(
    payload: PolicyViolationsIngest, db: Database = Depends(get_db)
) -> PolicyViolationsIngestResponse:
    """Bulk insert violations from the SDK. Returns count accepted.

    Webhook firing happens in the SDK, not here — by the time a
    violation reaches this endpoint, ``webhook_fired`` already
    reflects whether the SDK successfully called the webhook URL.
    """
    accepted = 0
    for v in payload.violations:
        # Deterministic id from (policy_name, trace_id, span_id) so an
        # SDK-side violation that's also caught by the server's own
        # ingest-time eval collapses to one row (PRIMARY KEY conflict
        # → ON CONFLICT DO NOTHING). Same idempotency guarantees apply
        # to OTel retries that re-POST the same violation.
        vid = _violation_id(v.policy_name, v.trace_id, v.span_id)
        # SSRF guard — blocked URLs don't get persisted, so the
        # dashboard can't show a poisoned link and a future webhook
        # firer can't be tricked into POSTing to instance-metadata.
        safe_webhook = (
            v.webhook_url
            if (not v.webhook_url or _webhook_url_safe(v.webhook_url))
            else None
        )
        db.execute(
            """
            INSERT INTO policy_violations (
                id, policy_name, policy_description, span_id, trace_id,
                condition_text, action_taken, severity, actual_value,
                webhook_fired, webhook_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                vid,
                v.policy_name,
                v.policy_description,
                v.span_id,
                v.trace_id,
                v.condition_text,
                v.action_taken,
                v.severity,
                v.actual_value,
                bool(v.webhook_fired or False),
                safe_webhook,
            ],
        )
        accepted += 1
    return PolicyViolationsIngestResponse(accepted=accepted)


@router.get("/v1/violations", response_model=PolicyViolationsResponse)
def list_violations(
    trace_id: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    policy_name: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> PolicyViolationsResponse:
    """List violations with optional filters."""
    where: list[str] = []
    params: list = []
    if trace_id:
        where.append("trace_id = ?")
        params.append(trace_id)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    if policy_name:
        where.append("policy_name = ?")
        params.append(policy_name)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    rows = db.fetchall_dict(
        f"""
        SELECT * FROM policy_violations
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    total_row = db.fetchone(
        f"SELECT COUNT(*) FROM policy_violations {where_clause}", params
    )
    total = int(total_row[0]) if total_row else 0

    return PolicyViolationsResponse(
        violations=[_row_to_violation(r) for r in rows],
        total=total,
    )


@router.get("/v1/violations/stats", response_model=PolicyViolationStats)
def violation_stats(db: Database = Depends(get_db)) -> PolicyViolationStats:
    """Aggregate counts. Cheap because the table is bounded by retention."""
    total_row = db.fetchone("SELECT COUNT(*) FROM policy_violations")
    total = int(total_row[0]) if total_row else 0

    by_sev_rows = db.fetchall(
        "SELECT severity, COUNT(*) FROM policy_violations GROUP BY severity"
    )
    by_pol_rows = db.fetchall(
        "SELECT policy_name, COUNT(*) FROM policy_violations GROUP BY policy_name"
    )

    return PolicyViolationStats(
        total=total,
        by_severity={(s or "unknown"): int(n) for s, n in by_sev_rows},
        by_policy={(p or "unknown"): int(n) for p, n in by_pol_rows},
    )


# ---- engine introspection / control ---------------------------------------


@router.get("/v1/policy/metrics")
def get_metrics() -> dict:
    """Snapshot of engine counters + p50/p99 eval latency.

    Designed for ad-hoc curl + dashboard polling. Doesn't persist
    anything — all numbers are in-process state. Restart resets.
    """
    return policy_metrics.snapshot().to_dict()


@router.post("/v1/policy/reload")
def post_reload() -> dict:
    """Force a hot-reload of LUMIN_POLICY_FILE.

    On invalid YAML the previous engine stays in place — production-
    safety choice over fail-open. Caller sees ``{"ok": false, ...}``
    and can re-fix the file before the next call.
    """
    return policy_runtime.reload_engine()


# ---- Policy CRUD (Phase 3 reads, Phase 4 writes) --------------------------


@router.get("/v1/policies", response_model=PolicyListResponse)
def list_policies(
    agent: Optional[str] = Query(
        None, description="Filter to policies that apply to this agent name"
    ),
    db: Database = Depends(get_db),
) -> PolicyListResponse:
    """List currently active policies.

    DB-backed when the engine is sourced from the policies table; falls
    through to the in-memory engine state when YAML is the source.

    The ``?agent=`` filter narrows to rules that would actually fire
    for events from that agent — the dashboard's AgentDetail page
    calls this to show the "Active policies" section.
    """
    source = policy_runtime.engine_source()

    # DB is authoritative for storage of every policy regardless of
    # which engine evaluates it. The SDK PolicyEngine handles
    # post_ingest rules; firewall.decide handles synchronous
    # lifecycles (before_proxy_call, before_tool_call, …). Both
    # read from the same `policies` table. If the table has any
    # rows (enabled OR disabled — soft-deleted ones still mean
    # the DB is the source of truth), it's authoritative; the
    # YAML-engine fallback only kicks in for the very-fresh case
    # where bootstrap hasn't written anything yet.
    has_any = bool(policy_store.has_any_policies(db))
    if has_any:
        db_rows = db.fetchall_dict(
            "SELECT * FROM policies WHERE enabled = true ORDER BY name"
        )
        out: list[PolicyOut] = []
        for r in db_rows:
            p = policy_store._row_to_policy(r)
            if agent and not p.applies_to_agent(agent):
                continue
            out.append(_policy_to_out(p, source="db", version=int(r.get("version") or 1)))
        return PolicyListResponse(policies=out, source="db", engine_loaded=True)

    # Empty `policies` table — fall through to the YAML-loaded
    # engine snapshot.
    eng = policy_runtime.get_engine()
    if eng is None:
        return PolicyListResponse(policies=[], source="none", engine_loaded=False)
    out2: list[PolicyOut] = []
    for p in eng.policies:
        if agent and not p.applies_to_agent(agent):
            continue
        out2.append(_policy_to_out(p, source=source))
    return PolicyListResponse(policies=out2, source=source, engine_loaded=True)


@router.get("/v1/policies/{name}", response_model=PolicyOut)
def get_policy(name: str, db: Database = Depends(get_db)) -> PolicyOut:
    """Single policy by name. DB row when DB-backed, else from the
    in-memory YAML engine state."""
    # Same DB-first logic as the list endpoint: a firewall-only
    # policy is still a real, persisted, viewable rule even though
    # the SDK PolicyEngine source is "none".
    source = policy_runtime.engine_source()
    row = db.fetchone_dict(
        "SELECT * FROM policies WHERE name = ? AND enabled = true", [name]
    )
    if row is not None:
        return _policy_to_out(
            policy_store._row_to_policy(row),
            source="db",
            version=int(row.get("version") or 1),
        )
    eng = policy_runtime.get_engine()
    if eng is None:
        raise HTTPException(status_code=404, detail=f"policy '{name}' not found")
    for p in eng.policies:
        if p.name == name:
            return _policy_to_out(p, source=source)
    raise HTTPException(status_code=404, detail=f"policy '{name}' not found")


@router.post("/v1/policies", response_model=PolicyOut, status_code=201)
def create_policy(
    body: PolicyCreate,
    request: Request,
    db: Database = Depends(get_db),
) -> PolicyOut:
    """Create a new policy. Validates against the engine's grammar
    (trigger / action / severity enums) AND parses the condition
    through SimpleEval to reject unparseable expressions.

    On success: writes the row, bumps the engine's version watcher,
    triggers an immediate reload so the new rule is live within the
    response. The dashboard sees the new policy without a poll."""
    p = _build_policy_from_create(body)
    actor = _resolve_actor(request)
    try:
        saved = policy_store.create_policy(db, p, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Atomic engine swap so subsequent /v1/spans calls fire against
    # the new rule. Hot-reload errors leave the previous engine in
    # place; we surface that to the caller via the reload_engine
    # error path so they know the write happened but eval is stale.
    reload_result = policy_runtime.reload_engine(db=db)
    if not reload_result.get("ok"):
        # Write succeeded; reload didn't. Log and continue — the
        # next watcher tick will retry.
        import logging
        logging.getLogger("lumin.api.policy").warning(
            "policy: post-create reload failed: %s", reload_result.get("error")
        )

    return _policy_to_out(saved, source="db", version=_lookup_version(db, saved.name))


@router.put("/v1/policies/{name}", response_model=PolicyOut)
def update_policy(
    name: str,
    body: PolicyUpdate,
    request: Request,
    db: Database = Depends(get_db),
) -> PolicyOut:
    """Partial update of a policy. Field-level — omitted fields keep
    their current value."""
    actor = _resolve_actor(request)

    # Validate enum fields when provided
    if body.trigger is not None and body.trigger not in VALID_TRIGGERS:
        raise HTTPException(
            status_code=400,
            detail=f"trigger must be one of {sorted(VALID_TRIGGERS)}",
        )
    if body.action is not None and body.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {sorted(VALID_ACTIONS)}",
        )
    if body.severity is not None and body.severity not in VALID_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"severity must be one of {sorted(VALID_SEVERITIES)}",
        )

    # If condition changed, validate it parses (cheap SimpleEval AST parse)
    if body.condition is not None:
        _validate_condition_parses(body.condition)

    try:
        saved = policy_store.update_policy(
            db, name,
            description=body.description,
            trigger=body.trigger,
            condition=body.condition,
            action=body.action,
            severity=body.severity,
            webhook_url=body.webhook_url,
            scope_agents=body.scope_agents,
            enabled=body.enabled,
            lifecycle=body.lifecycle,
            mode=body.mode,
            priority=body.priority,
            on_timeout=body.on_timeout,
            on_internal_error=body.on_internal_error,
            actor=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"policy '{name}' not found")
    except ValueError as e:
        # Validation error from policy_store (bad lifecycle/mode/etc.)
        raise HTTPException(status_code=400, detail=str(e))

    reload_result = policy_runtime.reload_engine(db=db)
    if not reload_result.get("ok"):
        import logging
        logging.getLogger("lumin.api.policy").warning(
            "policy: post-update reload failed: %s", reload_result.get("error")
        )
    return _policy_to_out(saved, source="db", version=_lookup_version(db, saved.name))


@router.delete("/v1/policies/{name}", status_code=204)
def delete_policy(
    name: str,
    request: Request,
    db: Database = Depends(get_db),
):
    """Soft-delete (enabled=false). The row stays in the table so
    historical violations can still resolve a policy_name → row;
    UPDATE with enabled=true revives it."""
    actor = _resolve_actor(request)
    deleted = policy_store.delete_policy(db, name, actor=actor)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"policy '{name}' not found")
    reload_result = policy_runtime.reload_engine(db=db)
    if not reload_result.get("ok"):
        import logging
        logging.getLogger("lumin.api.policy").warning(
            "policy: post-delete reload failed: %s", reload_result.get("error")
        )
    return None


@router.get("/v1/policies/{name}/audit", response_model=PolicyAuditResponse)
def policy_audit(
    name: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> PolicyAuditResponse:
    """Audit log for one policy. Most recent first."""
    rows, total = policy_store.list_audit(db, policy_name=name, limit=limit, offset=offset)
    entries = [
        PolicyAuditEntry(
            id=str(r["id"]),
            policy_name=r["policy_name"],
            action=r["action"],
            before=r.get("before"),
            after=r.get("after"),
            actor=r.get("actor"),
            created_at=r.get("created_at"),
        )
        for r in rows
    ]
    return PolicyAuditResponse(entries=entries, total=total)


# ---- /v1/policies/{name}/versions  (Slice 6C, §10.5) ---------------------


@router.get("/v1/policies/{name}/versions")
def list_policy_versions(
    name: str,
    limit: int = Query(100, ge=1, le=500),
    db: Database = Depends(get_db),
) -> dict:
    """Version snapshots — every create / update / rollback writes
    a row to ``policy_versions``. The dashboard's history tab
    renders this as a timeline with one-click rollback."""
    if policy_store.get_policy(db, name) is None:
        raise HTTPException(status_code=404, detail=f"policy {name!r} not found")
    versions = policy_store.list_versions(db, name, limit=limit)
    return {"policy_name": name, "versions": versions}


@router.get("/v1/policies/{name}/versions/{version_number}")
def get_policy_version(
    name: str,
    version_number: int,
    db: Database = Depends(get_db),
) -> dict:
    """Fetch a specific historical version (full YAML snapshot)."""
    snap = policy_store.get_version(db, name, version_number)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"policy {name!r} version {version_number} not found",
        )
    return snap


@router.post("/v1/policies/{name}/rollback")
def rollback_policy(
    name: str,
    payload: dict,
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    """Restore a policy to an earlier version snapshot.

    Body: ``{"version_number": N}``. Operator-attributed via the
    standard X-Lumin-Actor header so the rollback itself appears in
    the audit log."""
    version_number = payload.get("version_number")
    if not isinstance(version_number, int):
        raise HTTPException(
            status_code=400,
            detail="version_number must be an integer",
        )
    actor = request.headers.get("X-Lumin-Actor") or "rollback"
    try:
        restored = policy_store.rollback_to_version(
            db, name, version_number, actor=actor,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Reload the in-memory engine so the rollback is reflected in
    # the next decide() call without operators having to wait for
    # the watcher's tick.
    policy_runtime.maybe_reload_on_db_token_change()

    return {
        "rolled_back": name,
        "to_version": version_number,
        "current": _policy_to_out(restored, source="db", version=1).model_dump(),
    }


# ---- helpers --------------------------------------------------------------


def _policy_to_out(p, source: str = "yaml", version: int = 1) -> PolicyOut:
    """Project an SDK ``Policy`` dataclass onto the wire shape.

    Agent Firewall fields (lifecycle/mode/priority/on_timeout/...)
    are read with getattr-with-default so this remains compatible
    with old SDK Policy dataclasses missing those attrs (e.g. when
    a deployment hasn't redeployed the SDK yet but the dashboard
    is asking for the new fields).
    """
    return PolicyOut(
        name=p.name,
        description=p.description,
        trigger=p.trigger,
        condition=p.condition,
        action=p.action,
        severity=p.severity,
        webhook_url=p.webhook_url,
        scope_agents=list(p.scope_agents or []),
        enabled=True,
        source=source,
        version=version,
        lifecycle=getattr(p, "lifecycle", "post_ingest") or "post_ingest",
        mode=getattr(p, "mode", "enforce") or "enforce",
        priority=int(getattr(p, "priority", 0) or 0),
        on_timeout=getattr(p, "on_timeout", "allow") or "allow",
        on_internal_error=getattr(p, "on_internal_error", "allow") or "allow",
        circuit_breaker_state=getattr(p, "circuit_breaker_state", "ok") or "ok",
    )


def _build_policy_from_create(body: PolicyCreate) -> Policy:
    """Validate + materialize a Policy from a PolicyCreate body.

    Triple-validates: enum fields, condition parseability, and (via
    Policy dataclass) the rest. We do NOT trust the caller — same
    rules YAML must obey apply here.
    """
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if body.trigger not in VALID_TRIGGERS:
        raise HTTPException(
            status_code=400,
            detail=f"trigger must be one of {sorted(VALID_TRIGGERS)}",
        )
    if body.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {sorted(VALID_ACTIONS)}",
        )
    if body.severity not in VALID_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"severity must be one of {sorted(VALID_SEVERITIES)}",
        )
    if not body.condition or not body.condition.strip():
        raise HTTPException(status_code=400, detail="condition is required")

    _validate_condition_parses(body.condition)

    # Apply spec §10.1 default: new policies enter shadow mode unless
    # the caller explicitly overrides. The migration leaves existing
    # rows at mode='enforce' for back-compat; this branch only affects
    # the freshly-authored ones.
    mode = body.mode if body.mode is not None else "shadow"
    lifecycle = body.lifecycle if body.lifecycle is not None else "post_ingest"
    priority = int(body.priority) if body.priority is not None else 0
    on_timeout = body.on_timeout if body.on_timeout is not None else "allow"
    on_internal_error = (
        body.on_internal_error if body.on_internal_error is not None else "allow"
    )

    return Policy(
        name=body.name.strip(),
        description=body.description,
        trigger=body.trigger,
        condition=body.condition,
        action=body.action,
        severity=body.severity,
        webhook_url=body.webhook_url,
        scope_agents=list(body.scope_agents or []),
        lifecycle=lifecycle,
        mode=mode,
        priority=priority,
        on_timeout=on_timeout,
        on_internal_error=on_internal_error,
    )


_ALLOWED_NAMES = frozenset({
    "span", "trace",
    # Agent Firewall lifecycle namespaces — exposed by firewall.decide
    # to condition expressions for synchronous lifecycles. Validating
    # here keeps the editor's "save" path strict but accepts the
    # firewall vocab.
    "Input", "Output",
    "tool_name", "params", "text",
})
_ALLOWED_FUNCTIONS = frozenset({
    "len", "str", "int", "float", "abs",
    # Agent Firewall builtins — see firewall/builtins.py. These are
    # available to firewall-lifecycle conditions; allowing them in
    # the validator means operators can author rules through the
    # dashboard editor without hitting "function not defined" 400s.
    "regex_match", "regex_extract", "contains_any",
    "url_host", "url_in_allowlist", "is_internal_url", "is_destructive_path",
    "entropy", "len_chars",
    "looks_like_secret", "has_pii",
    "has_image_markdown_exfil", "has_ascii_smuggling",
    "redact_pii",
    # Cross-framework tool name canonicalization (Slice 2 Tier 1.1).
    "is_shell_tool", "is_web_fetch_tool", "is_db_write_tool",
    "is_filesystem_tool",
    # Presidio (Slice 2 Tier 2.1; optional dep — gracefully returns
    # 0.0 / [] when presidio-analyzer isn't installed)
    "presidio_pii_score", "presidio_pii_entities",
    # Prompt Guard 2 (Slice 3 Tier 2.2; optional dep — gracefully
    # returns 0.0 / "" when transformers / torch aren't installed)
    "prompt_guard_score", "prompt_guard_label",
    # Llama Guard 4 (Slice 3 Tier 2.3; optional dep — safe-by-default
    # results when transformers / torch / accelerate aren't installed)
    "llama_guard_classify", "llama_guard_unsafe", "llama_guard_categories",
    # IPI sniffer (Slice 3 PR L — §6.9; always-on, escalates with Prompt Guard 2)
    "ipi_score", "ipi_unsafe", "ipi_passages",
    # History-backed (registered per-request via build_history_builtins)
    "session_total_tokens", "session_total_cost",
    "trace_total_cost", "tool_calls_in_trace",
    "agent_calls_per_minute", "agent_calls_today",
    "pii_violations_in_project_last_24h",
    # Embedding similarity (Slice 3 Tier 2.4; DB-bound — operator
    # builds the corpus via /v1/firewall/corpora/* CRUD)
    "similar_to_corpus", "max_corpus_similarity",
    # Behavioral anomaly detector (Slice 3 PR Q — §11.4)
    "behavioral_anomaly_score",
    # LLM-as-judge (Slice 3 PR R — §6.7; opt-in via LUMIN_LLM_JUDGE_ENDPOINT)
    "llm_judge", "llm_judge_unsafe", "llm_judge_label",
    # Local fine-tuned classifier (Slice 3 PR S — §6.8 / §11.6;
    # opt-in via sklearn install + dashboard "Retrain classifier")
    "org_classifier_score", "org_classifier_predict",
})


def _validate_condition_parses(condition: str) -> None:
    """Raise HTTP 400 if the condition isn't a safe + valid simpleeval
    expression.

    Goes beyond a bare ``parse()`` (which accepts any syntactically
    valid Python expression including ``__import__("os")``). We walk
    the AST and reject:

      - Bare-name references outside ``{span, trace}`` — catches
        typos like ``trace.span_count > 0`` written under a span_end
        trigger, AND blocks ``__import__`` / ``open`` / ``exec`` at
        write time. simpleeval would refuse them at eval time too,
        but operators expect a 400 when they save a broken rule, not
        silently-skipped fires that look fine in the dashboard.
      - Function calls outside the engine's safe whitelist —
        ``foo(x)`` where ``foo`` isn't ``{len, str, int, float, abs}``.
        Same UX rationale.
    """
    try:
        from simpleeval import EvalWithCompoundTypes
    except ImportError:
        return
    try:
        EvalWithCompoundTypes().parse(condition)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"condition is not a valid expression: {e}"
        )

    import ast as _ast
    try:
        tree = _ast.parse(condition, mode="eval")
    except SyntaxError as e:
        raise HTTPException(
            status_code=400, detail=f"condition syntax error: {e}"
        )

    # Pass 1: identify every Name that's the direct target of a Call
    # so we know which Names are "being called as functions" vs "being
    # read as variables". Same Name node could only ever be one of
    # those (ast nodes are unique), so a small id() set is enough.
    call_func_names: set[int] = set()
    method_call_nodes: list = []
    bad_func_names: list = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            if isinstance(node.func, _ast.Name):
                call_func_names.add(id(node.func))
                if node.func.id not in _ALLOWED_FUNCTIONS:
                    bad_func_names.append(node.func.id)
            elif isinstance(node.func, _ast.Attribute):
                # Method calls are mostly disallowed (x.startswith(...),
                # __import__(...).system(...)) — reject at write time.
                # Exception: `.get(key, default)` on the Agent Firewall
                # namespaces (Input/Output/params). The decide engine
                # exposes these via _NS wrappers that translate the
                # attribute access into a real dict.get; validating
                # this AST shape here lets operators author firewall
                # rules with `Input.params.get("command", "")` without
                # tripping the method-call guard.
                if (
                    node.func.attr == "get"
                    and len(node.args) <= 2
                ):
                    # Walk leftmost name to confirm it's an allowed
                    # base. `Input.params.get(...)` → base is `Input`.
                    base = node.func.value
                    while isinstance(base, _ast.Attribute):
                        base = base.value
                    if isinstance(base, _ast.Name) and base.id in _ALLOWED_NAMES:
                        continue
                method_call_nodes.append(node)

    if bad_func_names:
        raise HTTPException(
            status_code=400,
            detail=(
                f"condition calls disallowed function "
                f"{bad_func_names[0]!r}; only "
                f"{sorted(_ALLOWED_FUNCTIONS)} are allowed"
            ),
        )
    if method_call_nodes:
        raise HTTPException(
            status_code=400,
            detail=(
                "method calls (e.g. x.startswith(...)) are not "
                "allowed in conditions"
            ),
        )

    # Pass 2: every other Name lookup must be in the variable allowlist
    # (span / trace). We deliberately allow function-call Names too —
    # those were already validated in pass 1.
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Name):
            continue
        if id(node) in call_func_names:
            continue
        if node.id in _ALLOWED_NAMES:
            continue
        if node.id in _ALLOWED_FUNCTIONS:
            # A function name used as a variable (e.g. ``f = len``) is
            # weird but technically harmless — the engine never exposes
            # the binding anyway. Reject to keep conditions readable.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"safe function {node.id!r} can only be called "
                    f"(e.g. {node.id}(x)), not referenced as a value"
                ),
            )
        raise HTTPException(
            status_code=400,
            detail=(
                f"condition references unknown identifier "
                f"{node.id!r}; only {sorted(_ALLOWED_NAMES)} are "
                f"available (plus the safe functions "
                f"{sorted(_ALLOWED_FUNCTIONS)})"
            ),
        )


def _lookup_version(db: Database, name: str) -> int:
    row = db.fetchone("SELECT version FROM policies WHERE name = ?", [name])
    return int(row[0]) if row and row[0] is not None else 1


def _resolve_actor(request: Request) -> str:
    """Best-effort actor identity for audit log.

    Prefers an ``X-Lumin-Actor`` header (operators set this from a CLI
    or a future auth proxy). Falls back to client host. Never blocks
    the write — audit is a best-effort artifact, not a security gate.
    """
    actor = request.headers.get("x-lumin-actor")
    if actor:
        return actor.strip()[:200]
    client = request.client
    if client and client.host:
        return f"http:{client.host}"
    return "anonymous"
