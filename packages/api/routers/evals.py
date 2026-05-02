import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from db import Database, get_db
from models import Eval, EvalCreate

router = APIRouter()


@router.post("/v1/evals", response_model=Eval)
def create_eval(payload: EvalCreate, db: Database = Depends(get_db)) -> Eval:
    eval_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        INSERT INTO evals (
            id, trace_id, span_id, name, score, label, comment, source, model, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            eval_id,
            payload.trace_id,
            payload.span_id,
            payload.name,
            payload.score,
            payload.label,
            payload.comment,
            payload.source or "manual",
            payload.model,
            created_at,
        ],
    )

    row = db.fetchone_dict("SELECT * FROM evals WHERE id = ?", [eval_id])
    return Eval(**row) if row else Eval(id=eval_id, trace_id=payload.trace_id)
