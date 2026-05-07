"""Prompt Guard 2 — Slice 3 PR G / spec §6.4.

Meta's Prompt-Guard-2 22M is a lightweight classifier that flags
prompt-injection / jailbreak attempts. It runs on CPU in <5ms and
catches the obvious cases (DAN-style "ignore previous instructions",
hidden instruction smuggling in retrieved context, role-play
prefixes that try to flip the assistant persona).

Optional dep — install with::

    pip install transformers torch

Not in the default install because (a) torch is large (~700MB), (b)
the model itself downloads ~90MB on first use, (c) plenty of Lumin
operators want the regex-only detection path. When the dep isn't
installed:

  - ``available = False`` at module load
  - ``prompt_guard_score(text)`` returns 0.0
  - Rules using the builtin silently never fire (Rule 7)

When the dep IS installed, the model loads lazily on first use
(NOT at import) so cold-start of the API stays fast.

Configuration:

  LUMIN_PROMPT_GUARD_MODEL — HF model id, defaults to
                              ``meta-llama/Prompt-Guard-2-22M``
  LUMIN_PROMPT_GUARD_DEVICE — ``cpu`` (default) or ``cuda``
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger("lumin.api.firewall.detectors.prompt_guard")


# Detect availability without importing the heavy modules — keeps
# import fast on installs without transformers.
try:
    import importlib.util as _ispec
    available = (
        _ispec.find_spec("transformers") is not None
        and _ispec.find_spec("torch") is not None
    )
except Exception:
    available = False


_MODEL_NAME = os.environ.get(
    "LUMIN_PROMPT_GUARD_MODEL", "meta-llama/Prompt-Guard-2-22M"
)
_DEVICE = os.environ.get("LUMIN_PROMPT_GUARD_DEVICE", "cpu")

# Lazy load — built on first call. Held under _model_lock to prevent
# concurrent first-call traffic from triggering N parallel model
# loads.
_pipeline = None
_model_lock = threading.Lock()
_load_failed = False  # latch — once we fail to load, stop retrying


def _ensure_loaded() -> bool:
    """Return True if the model is loaded + ready. Lazy + idempotent."""
    global _pipeline, _load_failed
    if _pipeline is not None:
        return True
    if _load_failed:
        return False
    if not available:
        return False
    with _model_lock:
        if _pipeline is not None:
            return True
        if _load_failed:
            return False
        try:
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForSequenceClassification,
                AutoTokenizer,
                pipeline,
            )
            tokenizer = AutoTokenizer.from_pretrained(_MODEL_NAME)
            model = AutoModelForSequenceClassification.from_pretrained(_MODEL_NAME)
            _pipeline = pipeline(
                "text-classification",
                model=model,
                tokenizer=tokenizer,
                device=_DEVICE,
                return_all_scores=True,
            )
            logger.info(
                "prompt_guard: loaded %s on %s", _MODEL_NAME, _DEVICE
            )
            return True
        except Exception:
            logger.exception(
                "prompt_guard: failed to load model %s — detector disabled "
                "for this process. Set LUMIN_PROMPT_GUARD_MODEL or pip "
                "install transformers torch.",
                _MODEL_NAME,
            )
            _load_failed = True
            return False


# ---- public API -----------------------------------------------------------


def prompt_guard_score(text: Optional[str]) -> float:
    """Return the model's confidence (0.0 - 1.0) that ``text`` is a
    prompt injection or jailbreak attempt. Safe defaults:

      - text is None / empty → 0.0
      - presidio not installed → 0.0
      - model load failed → 0.0
      - inference threw → 0.0

    Operators wire this into rule conditions like::

        prompt_guard_score(Input.last_user_msg) > 0.7
    """
    if not text or not isinstance(text, str):
        return 0.0
    if not _ensure_loaded():
        return 0.0
    try:
        # Truncate to model's expected max — Prompt-Guard-2-22M handles
        # ~512 tokens; over that the tokenizer truncates anyway, but
        # cap at 4000 chars defensively.
        snippet = text[:4000]
        results = _pipeline(snippet)  # type: ignore[operator]
        # results is List[List[dict]] when return_all_scores=True;
        # find the "INJECTION" or "JAILBREAK" label. Different model
        # versions use different label names — handle both.
        if not results:
            return 0.0
        scores = results[0] if isinstance(results[0], list) else results
        for entry in scores:
            label = str(entry.get("label", "")).upper()
            if label in ("INJECTION", "JAILBREAK", "MALICIOUS", "LABEL_1"):
                return float(entry.get("score", 0.0))
        # Fallback: highest non-BENIGN score.
        for entry in scores:
            label = str(entry.get("label", "")).upper()
            if label not in ("BENIGN", "LABEL_0"):
                return float(entry.get("score", 0.0))
        return 0.0
    except Exception:
        logger.exception("prompt_guard: inference failed")
        return 0.0


def prompt_guard_label(text: Optional[str]) -> str:
    """Return the predicted label (BENIGN | INJECTION | JAILBREAK)
    or empty string when unavailable."""
    if not text or not isinstance(text, str):
        return ""
    if not _ensure_loaded():
        return ""
    try:
        snippet = text[:4000]
        results = _pipeline(snippet)  # type: ignore[operator]
        if not results:
            return ""
        scores = results[0] if isinstance(results[0], list) else results
        # Pick the label with the highest score
        best = max(scores, key=lambda e: float(e.get("score", 0.0)))
        return str(best.get("label", ""))
    except Exception:
        logger.exception("prompt_guard: label inference failed")
        return ""


def reset_for_tests() -> None:
    """Test helper — clear the loaded model + the load-failed latch."""
    global _pipeline, _load_failed
    _pipeline = None
    _load_failed = False
