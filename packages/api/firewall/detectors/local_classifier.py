"""Local fine-tuned classifier — Slice 3 PR S / spec §6.8 + §11.6.

Trains a small classifier over the operator's own ``labels`` table
and exposes it as a builtin in policy expressions::

    condition: org_classifier_score(Input.last_user_msg, "injection") > 0.7
    action: block

The point: every operator's traffic is different, and the regex /
heuristic / generic-ML detectors can't catch org-specific attack
patterns ("a customer-support bot's idea of an unusual question"
isn't the same as a code-gen agent's). Once the operator labels
~100 examples via the dashboard's "mark as bad" button (PR #60's
labels infrastructure), they have enough data to train a simple
classifier that does catch their patterns.

Two backends shipped:

  1. **linear** (default): scikit-learn LogisticRegression on a
     TfidfVectorizer. Trains in <1 minute on a few hundred
     labels. Audit-friendly: the top weighted features are readable
     ("words that move the score"). Optional dep — sklearn.

  2. **distill** (heavier): DistilBERT fine-tune via
     transformers.Trainer. Trains in 5-30 min on CPU. Higher
     accuracy. Lands as a follow-up — this PR ships only the
     linear backend; the distill plumbing's already factored in
     the API so it can drop in cleanly.

Artifacts persist to ``firewall_classifier_artifacts`` table as
pickled bytes, keyed by ``(model_id, version)``. ``model_id``
defaults to ``"default"``; operators with multiple per-purpose
classifiers (one for injection, one for hallucination) use
distinct ids.

Provenance: every inference response includes ``model_version``,
``trained_at``, ``n_train_examples``, and ``n_features`` so the
dashboard can render "this decision used classifier v3 trained on
247 labels at 2026-05-08T12:33Z".

Inference is in-process, sub-millisecond after first load (the
classifier + vectorizer pickled together come up in ~20ms cold).
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("lumin.api.firewall.detectors.local_classifier")


# Schema for the classifier_artifacts table; created lazily on first
# train/save call. Keep the existing migrations module untouched —
# this lets the detector ship as an optional dep without forcing a
# DB migration on operators who never use it.
_SCHEMA_CREATED = False
_schema_lock = threading.Lock()


def _ensure_schema(db) -> None:
    global _SCHEMA_CREATED
    if _SCHEMA_CREATED:
        return
    with _schema_lock:
        if _SCHEMA_CREATED:
            return
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_classifier_artifacts (
                id INTEGER PRIMARY KEY,
                model_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                backend VARCHAR NOT NULL,
                trained_at TIMESTAMP NOT NULL,
                n_train_examples INTEGER NOT NULL,
                n_features INTEGER,
                metrics_json VARCHAR,
                payload BLOB NOT NULL,
                UNIQUE (model_id, version)
            )
            """
        )
        db.execute(
            "CREATE SEQUENCE IF NOT EXISTS firewall_classifier_seq START 1"
        )
        _SCHEMA_CREATED = True


# Detect sklearn availability. The detector is opt-in — operators
# who don't want to pull sklearn (~50MB) get the safe-by-default
# code path instead.
try:
    import importlib.util as _ispec
    available = _ispec.find_spec("sklearn") is not None
except Exception:
    available = False


# Process-local classifier cache: {(model_id, version): bundle}
_CACHE: Dict[Tuple[str, int], Dict[str, Any]] = {}
_CACHE_LATEST: Dict[str, int] = {}  # model_id -> latest version
_cache_lock = threading.Lock()


def _invalidate_cache(model_id: str) -> None:
    with _cache_lock:
        _CACHE_LATEST.pop(model_id, None)
        for k in list(_CACHE.keys()):
            if k[0] == model_id:
                _CACHE.pop(k, None)


# ---- training -----------------------------------------------------------


def train(
    db,
    *,
    model_id: str = "default",
    backend: str = "linear",
    min_examples: int = 20,
) -> Dict[str, Any]:
    """Train a fresh classifier over the labels table. Returns a
    summary dict with ``model_id``, ``version``, ``trained_at``,
    ``n_train_examples``, ``n_features``, ``metrics``.

    Raises:
      RuntimeError if sklearn isn't installed (operator opt-in)
      ValueError if there aren't enough labels (default min 20)
      ValueError on unknown ``backend``
    """
    if not available:
        raise RuntimeError(
            "sklearn not installed — pip install 'scikit-learn>=1.3' "
            "to enable the local classifier"
        )
    if backend not in ("linear",):
        raise ValueError(f"unsupported backend: {backend!r}; only 'linear' is shipped today")

    _ensure_schema(db)

    rows = db.fetchall_dict(
        """
        SELECT l.id, l.label, l.category, l.notes, l.field,
               l.span_id, s.input AS span_input, s.output AS span_output
        FROM labels l
        LEFT JOIN spans s ON s.id = l.span_id
        WHERE l.label IN ('bad', 'good')
        """,
    )
    if len(rows) < min_examples:
        raise ValueError(
            f"only {len(rows)} labels available; need at least "
            f"{min_examples} to train. Label more traces via "
            f"the dashboard's 'Mark as ...' buttons first."
        )

    # Project labels onto the text field they describe.
    texts: List[str] = []
    targets: List[int] = []  # 1 = bad, 0 = good
    for r in rows:
        text = _project_text(r)
        if not text:
            continue
        texts.append(text)
        targets.append(1 if r.get("label") == "bad" else 0)

    if len(set(targets)) < 2:
        raise ValueError(
            "labels span only one class; need both 'bad' and 'good' "
            "examples to train a classifier"
        )

    bundle = _train_linear(texts, targets)

    # Persist + atomic swap. New version = max + 1.
    last = db.fetchone(
        "SELECT MAX(version) FROM firewall_classifier_artifacts WHERE model_id = ?",
        [model_id],
    )
    version = int((last[0] or 0)) + 1
    new_id = db.fetchone("SELECT nextval('firewall_classifier_seq')")[0]
    payload = pickle.dumps(bundle)
    metrics = bundle.pop("_metrics", {})
    metrics_json = json.dumps(metrics)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        INSERT INTO firewall_classifier_artifacts (
            id, model_id, version, backend, trained_at,
            n_train_examples, n_features, metrics_json, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            int(new_id), model_id, version, backend, now,
            len(texts), bundle.get("n_features"),
            metrics_json, payload,
        ],
    )
    _invalidate_cache(model_id)

    return {
        "model_id": model_id,
        "version": version,
        "backend": backend,
        "trained_at": now.isoformat(),
        "n_train_examples": len(texts),
        "n_features": bundle.get("n_features"),
        "metrics": metrics,
    }


def _project_text(row: Dict[str, Any]) -> str:
    """Pick the text that the label refers to.
    field=input → span_input; field=output → span_output. Falls back
    to whichever is non-empty when the explicit field is missing."""
    field = (row.get("field") or "").lower()
    inp = row.get("span_input")
    out = row.get("span_output")
    if field in ("input", "tool_params") and inp:
        return _to_text(inp)
    if field in ("output", "tool_result") and out:
        return _to_text(out)
    return _to_text(out or inp or "")


def _to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, default=str)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _train_linear(texts: List[str], targets: List[int]) -> Dict[str, Any]:
    """Train the linear backend. Returns a bundle dict with
    ``vectorizer``, ``model``, ``n_features``, plus a transient
    ``_metrics`` key the caller pops + persists separately."""
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-not-found]
    from sklearn.linear_model import LogisticRegression  # type: ignore[import-not-found]
    from sklearn.metrics import accuracy_score  # type: ignore[import-not-found]

    vec = TfidfVectorizer(
        ngram_range=(1, 2),
        max_features=10_000,
        min_df=1,
        lowercase=True,
    )
    X = vec.fit_transform(texts)
    clf = LogisticRegression(
        max_iter=1000, class_weight="balanced",
    )
    clf.fit(X, targets)

    # Quick training-set accuracy for the metrics dashboard. We
    # don't hold out a test set with this little data — operators
    # can compare versions on labels added AFTER a train run.
    preds = clf.predict(X)
    train_acc = float(accuracy_score(targets, preds))

    return {
        "vectorizer": vec,
        "model": clf,
        "n_features": int(X.shape[1]),
        "_metrics": {
            "train_accuracy": train_acc,
            "n_examples": len(texts),
            "class_balance": {
                "bad": int(sum(targets)),
                "good": int(len(targets) - sum(targets)),
            },
        },
    }


# ---- inference ----------------------------------------------------------


def _load_latest(db, model_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the latest classifier bundle for ``model_id``. Cached
    in process; cache is invalidated on train()."""
    with _cache_lock:
        latest_v = _CACHE_LATEST.get(model_id)
        if latest_v is not None:
            cached = _CACHE.get((model_id, latest_v))
            if cached is not None:
                return cached
    _ensure_schema(db)
    row = db.fetchone_dict(
        """
        SELECT version, payload, trained_at, n_train_examples,
               n_features, backend, metrics_json
        FROM firewall_classifier_artifacts
        WHERE model_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        [model_id],
    )
    if not row:
        return None
    try:
        bundle = pickle.loads(row["payload"])
    except Exception:
        logger.exception("local_classifier: failed to unpickle %s", model_id)
        return None
    bundle["model_id"] = model_id
    bundle["version"] = int(row["version"])
    bundle["backend"] = row.get("backend") or "linear"
    bundle["trained_at"] = (
        row["trained_at"].isoformat() if row.get("trained_at") else None
    )
    bundle["n_train_examples"] = int(row.get("n_train_examples") or 0)
    if "n_features" not in bundle and row.get("n_features"):
        bundle["n_features"] = int(row["n_features"])
    try:
        bundle["metrics"] = json.loads(row["metrics_json"]) if row.get("metrics_json") else {}
    except (ValueError, TypeError):
        bundle["metrics"] = {}
    with _cache_lock:
        _CACHE_LATEST[model_id] = bundle["version"]
        _CACHE[(model_id, bundle["version"])] = bundle
    return bundle


def org_classifier_score(
    db, text: Optional[str], model_id: str = "default",
) -> float:
    """Probability (0.0-1.0) that ``text`` is class=bad per the
    operator's local classifier. Returns 0.0 when no classifier is
    trained / sklearn unavailable / inference fails (Rule 7)."""
    if not text or not isinstance(text, str):
        return 0.0
    if not available:
        return 0.0
    try:
        bundle = _load_latest(db, model_id)
        if not bundle:
            return 0.0
        vec = bundle.get("vectorizer")
        clf = bundle.get("model")
        if vec is None or clf is None:
            return 0.0
        X = vec.transform([text[:8000]])
        # predict_proba returns [[P(class=0), P(class=1)]]; we want
        # P(class=1) i.e. probability of being labeled bad.
        proba = clf.predict_proba(X)[0]
        return float(proba[1]) if len(proba) > 1 else 0.0
    except Exception:
        logger.exception("local_classifier: inference failed")
        return 0.0


def org_classifier_predict(
    db, text: Optional[str], model_id: str = "default",
) -> Dict[str, Any]:
    """Full prediction with provenance for the dashboard. Returns::

        {
          "label": "bad" | "good" | "unknown",
          "score": float,
          "model_id": str,
          "version": int,
          "trained_at": str,
          "n_train_examples": int,
          "n_features": int,
          "ok": bool,
        }
    """
    safe = {
        "label": "unknown", "score": 0.0,
        "model_id": model_id, "version": 0,
        "trained_at": None, "n_train_examples": 0,
        "n_features": 0, "ok": False,
    }
    if not text or not isinstance(text, str) or not available:
        return safe
    bundle = _load_latest(db, model_id)
    if not bundle:
        return safe
    score = org_classifier_score(db, text, model_id)
    return {
        "label": "bad" if score >= 0.5 else "good",
        "score": score,
        "model_id": model_id,
        "version": bundle["version"],
        "trained_at": bundle.get("trained_at"),
        "n_train_examples": bundle.get("n_train_examples", 0),
        "n_features": bundle.get("n_features", 0),
        "ok": True,
    }


def list_models(db) -> List[Dict[str, Any]]:
    """All trained models — one row per (model_id, version). Used by
    the dashboard's classifier page."""
    _ensure_schema(db)
    rows = db.fetchall_dict(
        """
        SELECT model_id, version, backend, trained_at,
               n_train_examples, n_features, metrics_json
        FROM firewall_classifier_artifacts
        ORDER BY model_id ASC, version DESC
        """,
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            metrics = json.loads(r["metrics_json"]) if r.get("metrics_json") else {}
        except (ValueError, TypeError):
            metrics = {}
        out.append({
            "model_id": r.get("model_id"),
            "version": int(r.get("version") or 0),
            "backend": r.get("backend") or "linear",
            "trained_at": (
                r["trained_at"].isoformat() if r.get("trained_at") else None
            ),
            "n_train_examples": int(r.get("n_train_examples") or 0),
            "n_features": int(r.get("n_features") or 0),
            "metrics": metrics,
        })
    return out


def reset_for_tests() -> None:
    """Test helper — clear the cache + schema flag."""
    global _SCHEMA_CREATED
    with _cache_lock:
        _CACHE.clear()
        _CACHE_LATEST.clear()
    _SCHEMA_CREATED = False
