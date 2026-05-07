"""Agent Firewall HTTP surface.

Implements §5 of ``docs/AGENT_FIREWALL_SPEC.md``:

  - POST /v1/policy/decide          (§5.1)
  - GET  /v1/decisions              (§5.2)
  - GET  /v1/decisions/{id}         (§5.3)
  - POST /v1/policies/{name}/mode   (§5.4)
  - POST /v1/firewall/panic_disable (§10.2)
  - GET  /v1/firewall/panic_disable
  - POST /v1/approvals/{id}/resolve (§5.7)
  - GET  /v1/approvals              (§5.6)

The decide engine itself lives in ``firewall.decide``; this module
is the boundary between FastAPI request lifecycles and the engine.

Per Rule 7, the decide endpoint NEVER returns a 5xx — even on
internal errors, the JSON response is a permissive ``allow``. The
read endpoints follow the existing convention of bubbling 500s.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import policy_runtime
import policy_store
from db import Database, get_db

from firewall import decide as fw_decide
from firewall import suggester as fw_suggester
from firewall.templates import loader as fw_templates
from models import TemplateInstantiateRequest

logger = logging.getLogger("lumin.api.routers.firewall")

router = APIRouter()


# ---- request/response models ---------------------------------------------


class DecideRequest(BaseModel):
    lifecycle: str
    tool_name: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    session_id: Optional[str] = None
    agent: Optional[str] = None
    project: Optional[str] = None
    model: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    output: Optional[Any] = None


class ModeChangeRequest(BaseModel):
    mode: str = Field(..., description="One of: shadow, flag, enforce")


class PanicRequest(BaseModel):
    disabled: bool
    reason: Optional[str] = None
    actor: Optional[str] = None


class ApprovalResolveRequest(BaseModel):
    resolution: str = Field(..., description="One of: allow, deny")
    reason: Optional[str] = None
    resolver: Optional[str] = None


# ---- POST /v1/policy/decide  (§5.1) ---------------------------------------


@router.post("/v1/policy/decide")
def decide_endpoint(
    payload: DecideRequest, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    """Synchronous decision endpoint. Never raises — Rule 7."""
    try:
        return fw_decide.decide(
            db,
            lifecycle=payload.lifecycle,
            tool_name=payload.tool_name,
            params=payload.params,
            trace_id=payload.trace_id,
            span_id=payload.span_id,
            session_id=payload.session_id,
            agent=payload.agent,
            project=payload.project,
            model=payload.model,
            messages=payload.messages,
            output=payload.output,
        )
    except Exception:
        logger.exception("firewall: /v1/policy/decide crashed")
        return {
            "decision": "allow",
            "reason": "internal_error",
            "duration_ms": 0,
        }


# ---- GET /v1/decisions  (§5.2) -------------------------------------------


@router.get("/v1/decisions")
def list_decisions(
    project: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    decision: Optional[str] = Query(None),
    lifecycle: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    where, params = _build_decision_filters(
        project=project, agent=agent, session_id=session_id,
        trace_id=trace_id, decision=decision, lifecycle=lifecycle,
        since=since, until=until,
    )
    rows = db.fetchall_dict(
        f"""
        SELECT * FROM decisions {where}
        ORDER BY decision_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    total_row = db.fetchone(
        f"SELECT COUNT(*) FROM decisions {where}", params
    )
    total = int(total_row[0]) if total_row else 0
    return {
        "decisions": [_decision_row_to_dict(r) for r in rows],
        "total": total,
        "has_more": (offset + len(rows)) < total,
    }


# ---- GET /v1/decisions/{id}  (§5.3) --------------------------------------


@router.get("/v1/decisions/{decision_id}")
def get_decision(decision_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    row = db.fetchone_dict(
        "SELECT * FROM decisions WHERE id = ?", [decision_id]
    )
    if not row:
        raise HTTPException(status_code=404, detail="decision not found")

    siblings: List[Dict[str, Any]] = []
    if row.get("trace_id"):
        sib_rows = db.fetchall_dict(
            """
            SELECT * FROM decisions
            WHERE trace_id = ? AND id != ?
            ORDER BY decision_at ASC
            """,
            [row["trace_id"], decision_id],
        )
        siblings = [_decision_row_to_dict(r) for r in sib_rows]

    policy_snapshot = None
    pol_name = row.get("policy_name")
    if pol_name and pol_name != "_engine_":
        # Best-effort: the matched policy at the time of the decision.
        # Use the current row — version-snapshot recovery from
        # policy_versions can land in a later slice.
        p = policy_store.get_policy(db, pol_name)
        if p is not None:
            policy_snapshot = {
                "name": p.name,
                "description": p.description,
                "lifecycle": p.lifecycle,
                "mode": p.mode,
                "action": p.action,
                "severity": p.severity,
                "condition": p.condition,
                "priority": p.priority,
            }

    return {
        "decision": _decision_row_to_dict(row),
        "policy": policy_snapshot,
        "siblings": siblings,
    }


# ---- POST /v1/policies/{name}/mode  (§5.4) -------------------------------


@router.post("/v1/policies/{name}/mode")
def set_policy_mode(
    name: str, payload: ModeChangeRequest, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    if payload.mode not in policy_store.VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {sorted(policy_store.VALID_MODES)}",
        )
    before = policy_store.get_policy(db, name)
    if before is None:
        raise HTTPException(status_code=404, detail=f"policy {name!r} not found")
    previous_mode = before.mode

    # Forecast: how often did this policy fire in shadow over the
    # last 30 days, and on which traces? Run BEFORE flipping so the
    # caller sees the back-test before they commit.
    forecast = _forecast_for_mode_change(db, policy_name=name, target_mode=payload.mode)

    after = policy_store.update_policy(db, name, mode=payload.mode)
    # Policies-table token bumped → engine reload picks the change up
    # on its next maybe_reload_on_db_token_change tick.
    policy_runtime.maybe_reload_on_db_token_change()

    return {
        "id": after.name,
        "mode": after.mode,
        "previous_mode": previous_mode,
        "forecast": forecast,
    }


def _forecast_for_mode_change(
    db: Database, *, policy_name: str, target_mode: str
) -> Dict[str, Any]:
    """Back-test the policy against the last 30d of decisions.

    The pertinent question is "if we'd been in target_mode for the
    last 30d, how often would we have actually blocked?" We answer it
    by counting decisions where this policy fired with a non-allow
    action — that's the population. ``examples`` returns up to 3
    representative trace_ids for the dashboard preview.
    """
    try:
        cnt_row = db.fetchone(
            """
            SELECT COUNT(*) FROM decisions
            WHERE policy_name = ?
              AND decision IN ('block', 'flag', 'require_approval', 'rewrite')
              AND decision_at >= NOW() - INTERVAL '30 days'
            """,
            [policy_name],
        )
        ex_rows = db.fetchall_dict(
            """
            SELECT trace_id FROM decisions
            WHERE policy_name = ?
              AND trace_id IS NOT NULL
              AND decision IN ('block', 'flag', 'require_approval', 'rewrite')
              AND decision_at >= NOW() - INTERVAL '30 days'
            ORDER BY decision_at DESC
            LIMIT 3
            """,
            [policy_name],
        )
    except Exception:
        logger.exception("firewall: forecast query failed")
        return {"would_have_blocked": 0, "examples": []}
    return {
        "would_have_blocked": int(cnt_row[0]) if cnt_row else 0,
        "examples": [r["trace_id"] for r in ex_rows if r.get("trace_id")],
    }


# ---- POST /v1/firewall/panic_disable  (§10.2) ----------------------------


@router.post("/v1/firewall/panic_disable")
def set_panic_disabled(
    payload: PanicRequest, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    """Operator big-red-button: turn off all enforcement immediately.

    Persisted to ``firewall_kv`` so a restart picks the disabled state
    back up. Re-enabling is the same endpoint with disabled=false.

    Auditable — writes a row in ``firewall_panic_audit`` (created
    inline if absent so the panic surface doesn't depend on a
    migration the operator might not have run yet).
    """
    blob = json.dumps({"disabled": bool(payload.disabled), "reason": payload.reason})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.execute(
            """
            INSERT INTO firewall_kv (k, v, updated_at, updated_by)
            VALUES ('panic_disabled', ?, ?, ?)
            ON CONFLICT (k) DO UPDATE SET
                v = EXCLUDED.v,
                updated_at = EXCLUDED.updated_at,
                updated_by = EXCLUDED.updated_by
            """,
            [blob, now, payload.actor],
        )
    except Exception:
        logger.exception("firewall: panic write failed")

    # Audit row — best effort, separate table.
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_panic_audit (
                id BIGINT PRIMARY KEY,
                disabled BOOLEAN NOT NULL,
                reason VARCHAR,
                actor VARCHAR,
                changed_at TIMESTAMP NOT NULL
            )
            """
        )
        next_id_row = db.fetchone("SELECT COALESCE(MAX(id), 0) + 1 FROM firewall_panic_audit")
        next_id = int(next_id_row[0]) if next_id_row else 1
        db.execute(
            "INSERT INTO firewall_panic_audit VALUES (?, ?, ?, ?, ?)",
            [next_id, bool(payload.disabled), payload.reason, payload.actor, now],
        )
    except Exception:
        logger.exception("firewall: panic audit write failed")

    fw_decide.set_panic_disabled(payload.disabled, payload.reason)
    return {
        "disabled": bool(payload.disabled),
        "reason": payload.reason,
        "updated_at": now.isoformat(),
        "updated_by": payload.actor,
    }


@router.get("/v1/firewall/panic_disable")
def get_panic_disabled(db: Database = Depends(get_db)) -> Dict[str, Any]:
    fw_decide.refresh_panic_state(db)
    return {
        "disabled": fw_decide.is_panic_disabled(),
    }


# ---- approvals  (§5.6, §5.7) ---------------------------------------------


@router.get("/v1/approvals")
def list_approvals(
    state: str = Query("pending"),
    project: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    clauses: List[str] = ["state = ?"]
    params: List[Any] = [state]
    if agent:
        clauses.append("agent = ?"); params.append(agent)
    where = "WHERE " + " AND ".join(clauses)
    rows = db.fetchall_dict(
        f"""
        SELECT * FROM approvals {where}
        ORDER BY requested_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    total_row = db.fetchone(f"SELECT COUNT(*) FROM approvals {where}", params)
    return {
        "approvals": [_approval_row_to_dict(r) for r in rows],
        "total": int(total_row[0]) if total_row else 0,
    }


@router.get("/v1/approvals/{approval_id}")
def get_approval(approval_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    row = db.fetchone_dict("SELECT * FROM approvals WHERE id = ?", [approval_id])
    if not row:
        raise HTTPException(status_code=404, detail="approval not found")
    return _approval_row_to_dict(row)


@router.post("/v1/approvals/{approval_id}/resolve")
def resolve_approval(
    approval_id: str,
    payload: ApprovalResolveRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    if payload.resolution not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="resolution must be allow|deny")
    # Pull richer context up-front — needed both for the state guard
    # and (on deny) to populate the session deny cache so the LLM
    # can't immediately retry the same tuple.
    row = db.fetchone_dict(
        """
        SELECT a.id, a.state, a.policy_id, a.tool_name, a.params_truncated,
               a.decision_id, d.session_id
        FROM approvals a
        LEFT JOIN decisions d ON d.id = a.decision_id
        WHERE a.id = ?
        """,
        [approval_id],
    )
    if not row:
        raise HTTPException(status_code=404, detail="approval not found")
    if row["state"] not in ("pending",):
        raise HTTPException(
            status_code=409,
            detail=f"approval already {row['state']}",
        )
    new_state = "allowed" if payload.resolution == "allow" else "denied"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        UPDATE approvals SET
            state = ?,
            resolved_at = ?,
            resolved_by = ?,
            resolution_reason = ?
        WHERE id = ?
        """,
        [new_state, now, payload.resolver, payload.reason, approval_id],
    )

    # Slice 2 Tier 1.5(b): record deny in session cache so the LLM's
    # next retry of the same (session, tool, params) tuple gets an
    # immediate auto-deny instead of pinging an admin again. Best
    # effort — params_truncated is JSON-stringified, parse defensively.
    if new_state == "denied" and row.get("session_id") and row.get("tool_name"):
        try:
            params_blob = row.get("params_truncated")
            if isinstance(params_blob, str):
                try:
                    params = json.loads(params_blob)
                except (json.JSONDecodeError, TypeError):
                    params = None
            elif isinstance(params_blob, dict):
                params = params_blob
            else:
                params = None
            cache_key = fw_decide._deny_cache_key(
                row["session_id"], row["tool_name"], params,
            )
            fw_decide._record_deny_in_cache(
                cache_key, row["policy_id"] or "_operator_deny",
            )
        except Exception:
            logger.exception(
                "firewall: failed to record deny in session cache "
                "(approval=%s); operator deny still applied",
                approval_id,
            )

    return {
        "id": approval_id,
        "state": new_state,
        "resolved_at": now.isoformat(),
    }


# ---- helpers --------------------------------------------------------------


def _build_decision_filters(
    *,
    project: Optional[str], agent: Optional[str], session_id: Optional[str],
    trace_id: Optional[str], decision: Optional[str], lifecycle: Optional[str],
    since: Optional[datetime], until: Optional[datetime],
) -> tuple:
    clauses: List[str] = []
    params: List[Any] = []
    if project:
        clauses.append("project = ?"); params.append(project)
    if agent:
        clauses.append("agent = ?"); params.append(agent)
    if session_id:
        clauses.append("session_id = ?"); params.append(session_id)
    if trace_id:
        clauses.append("trace_id = ?"); params.append(trace_id)
    if decision:
        clauses.append("decision = ?"); params.append(decision)
    if lifecycle:
        clauses.append("lifecycle = ?"); params.append(lifecycle)
    if since:
        clauses.append("decision_at >= ?"); params.append(since.replace(tzinfo=None))
    if until:
        clauses.append("decision_at <= ?"); params.append(until.replace(tzinfo=None))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _decision_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    da = out.get("decision_at")
    if isinstance(da, datetime):
        out["decision_at"] = da.isoformat()
    if isinstance(out.get("metadata"), str):
        try:
            out["metadata"] = json.loads(out["metadata"])
        except json.JSONDecodeError:
            pass
    return out


def _approval_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("requested_at", "resolved_at", "timeout_at"):
        v = out.get(key)
        if isinstance(v, datetime):
            out[key] = v.isoformat()
    if isinstance(out.get("params_truncated"), str):
        try:
            out["params_truncated"] = json.loads(out["params_truncated"])
        except json.JSONDecodeError:
            pass
    return out


# ---- templates  (§8 dashboard, Slice 2 Tier 1.05) -----------------------


@router.get("/v1/firewall/templates")
def list_templates() -> Dict[str, Any]:
    """All available rule templates. Cheap — registry is loaded once at
    module load. Returns a compact summary; the dashboard fetches the
    full template (with fields) on click via the {id} endpoint."""
    return {"templates": fw_templates.list_templates_summary()}


@router.get("/v1/firewall/templates/{template_id}")
def get_template_detail(template_id: str) -> Dict[str, Any]:
    """Full template — fields schema, defaults, condition string. The
    dashboard's modal form renders ``fields`` as a form."""
    tpl = fw_templates.get_template(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"template {template_id!r} not found")
    return tpl


@router.post("/v1/firewall/templates/{template_id}/instantiate")
def instantiate_template(
    template_id: str,
    payload: TemplateInstantiateRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Create a policy from a template + operator's field values.

    Always lands in mode=shadow per §10.1 unless operator explicitly
    asks for another mode. Triggers an engine reload so the new rule
    is live in the same response — same pattern as POST /v1/policies.
    """
    if not payload.name or not payload.name.strip():
        raise HTTPException(status_code=400, detail="name is required")

    try:
        policy = fw_templates.compile_rule(
            template_id, payload.name.strip(),
            payload.field_values,
            mode=payload.mode,
        )
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"template {template_id!r} not found"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        saved = policy_store.create_policy(db, policy, actor="template_instantiate")
    except ValueError as e:
        # Name conflict OR firewall-field validation error.
        raise HTTPException(status_code=409, detail=str(e))

    # Hot-reload so the new rule is live immediately.
    try:
        policy_runtime.reload_engine(db=db)
    except Exception:
        logger.exception("template instantiate: post-create reload failed")

    return {
        "name": saved.name,
        "lifecycle": getattr(saved, "lifecycle", "post_ingest"),
        "mode": getattr(saved, "mode", "shadow"),
        "action": saved.action,
        "severity": saved.severity,
        "condition": saved.condition,
        "description": saved.description,
        "template_id": template_id,
    }


# ---- labels  (§5.8 — Slice 3 PR C foundation) ---------------------------


class LabelRequest(BaseModel):
    """Body for POST /v1/labels — operator marks a trace/span as bad/good
    for downstream classifier training and false-positive review."""
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    decision_id: Optional[str] = None
    field: str = Field(..., description="One of: input, output, tool_params, tool_result")
    label: str = Field(..., description="One of: bad, good, neutral")
    category: Optional[str] = None
    notes: Optional[str] = None
    labeled_by: Optional[str] = "dashboard"


@router.post("/v1/labels")
def post_label(payload: LabelRequest, db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Insert a label row. Used by the dashboard's 'Mark as false
    positive' button on /decisions/{id}, and as the substrate for
    the local classifier trainer (Slice 4)."""
    if payload.label not in ("bad", "good", "neutral"):
        raise HTTPException(status_code=400, detail="label must be bad/good/neutral")
    if payload.field not in ("input", "output", "tool_params", "tool_result"):
        raise HTTPException(
            status_code=400,
            detail="field must be one of: input, output, tool_params, tool_result",
        )
    import uuid as _uuid
    label_id = "lbl_" + _uuid.uuid4().hex[:24]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.execute(
            """
            INSERT INTO labels (
                id, trace_id, span_id, field, label, category, notes,
                labeled_by, labeled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                label_id, payload.trace_id, payload.span_id,
                payload.field, payload.label, payload.category,
                payload.notes, payload.labeled_by or "dashboard", now,
            ],
        )
    except Exception:
        logger.exception("firewall: failed to insert label row")
        raise HTTPException(status_code=500, detail="label insert failed")
    return {
        "id": label_id,
        "label": payload.label,
        "labeled_at": now.isoformat(),
    }


# ---- pattern suggester (§5.5 — Slice 3 PR D) ----------------------------


class SuggestRequest(BaseModel):
    """Body for POST /v1/policies/suggest. Operator clicks "Block this
    pattern" on a fired decision; we synthesize a draft policy."""
    decision_id: str


class PromoteSuggestionRequest(BaseModel):
    """Body for POST /v1/policies/suggest/{id}/promote. The operator
    can rename the policy at promotion time — the auto-generated name
    is fine but operators usually want something descriptive."""
    name: str


@router.post("/v1/policies/suggest")
def suggest_policy(payload: SuggestRequest, db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Generate a draft policy from a fired decision. Persists the
    suggestion in the pattern_suggestions table so operators can
    revisit / dismiss / promote later."""
    try:
        return fw_suggester.suggest_from_decision(db, payload.decision_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/v1/policies/suggest/{suggestion_id}")
def get_suggestion(suggestion_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    out = fw_suggester.get_suggestion(db, suggestion_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"suggestion {suggestion_id!r} not found")
    return out


@router.post("/v1/policies/suggest/{suggestion_id}/promote")
def promote_suggestion(
    suggestion_id: str,
    payload: PromoteSuggestionRequest,
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Promote a suggestion into a real policy. Lands in mode=shadow."""
    try:
        saved = fw_suggester.promote_suggestion(db, suggestion_id, payload.name.strip())
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Reload engine so the new rule is live.
    try:
        policy_runtime.reload_engine(db=db)
    except Exception:
        logger.exception("suggester: post-promote reload failed")

    return {
        "name": saved.name,
        "lifecycle": getattr(saved, "lifecycle", "post_ingest"),
        "mode": getattr(saved, "mode", "shadow"),
        "action": saved.action,
        "severity": saved.severity,
        "condition": saved.condition,
        "suggestion_id": suggestion_id,
    }


@router.post("/v1/policies/suggest/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: str, db: Database = Depends(get_db)) -> Dict[str, Any]:
    fw_suggester.dismiss_suggestion(db, suggestion_id)
    return {"id": suggestion_id, "dismissed": True}


# ---- frequent-pattern miner (§11.3 — Slice 3 PR E) ----------------------


@router.post("/v1/firewall/miner/run")
def run_miner(db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Manual trigger — kicks the frequent-pattern miner on demand.
    Same scan + emit logic as the background loop. Returns a summary
    of what it did. Useful for ad-hoc operator-driven scans
    (\"refresh suggestions now\")."""
    from firewall import miner as fw_miner
    return fw_miner.mine_recent_patterns(db)


@router.get("/v1/firewall/suggestions")
def list_suggestions(
    state: str = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Pending pattern suggestions for the dashboard inbox.
    state ∈ pending | promoted | dismissed | all."""
    where = ""
    if state == "pending":
        where = "WHERE promoted_to_policy_id IS NULL AND dismissed_at IS NULL"
    elif state == "promoted":
        where = "WHERE promoted_to_policy_id IS NOT NULL"
    elif state == "dismissed":
        where = "WHERE dismissed_at IS NOT NULL"
    rows = db.fetchall_dict(
        f"""
        SELECT * FROM pattern_suggestions {where}
        ORDER BY suggested_at DESC LIMIT ?
        """,
        [limit],
    )
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "template": r.get("template"),
            "draft_yaml": r.get("draft_yaml"),
            "suggested_at": (
                r.get("suggested_at").isoformat()
                if r.get("suggested_at") else None
            ),
            "promoted_to_policy_id": r.get("promoted_to_policy_id"),
            "dismissed_at": (
                r.get("dismissed_at").isoformat()
                if r.get("dismissed_at") else None
            ),
            "forecast_fp_count": int(r.get("forecast_fp_count") or 0),
            "source_violation_id": r.get("source_violation_id"),
        })
    return {"suggestions": out, "total": len(out)}
