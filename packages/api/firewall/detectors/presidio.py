"""Microsoft Presidio integration — Tier 2.1 of SLICE_2_PLAN.md.

Semantic PII detection that complements the regex-pack shape detection
in ``regex_pack.py``. Presidio is heavier (it loads a spaCy NLP model
on first use, ~500MB resident) but catches semantic PII that pure
regex misses — most importantly **person names and addresses**, which
have no useful regex shape.

Design constraints:

  - **Optional dep.** Presidio brings spaCy + language models that
    inflate the install footprint substantially. Operators who don't
    want it shouldn't have to install it. If the import fails at
    module load time, ``available = False`` and the public functions
    return safe defaults — score 0.0, empty entity list. Rule 7: a
    missing classifier never blocks an agent.
  - **Lazy init.** ``AnalyzerEngine()`` instantiation is the slow
    step (~2s on a fresh process loading the spaCy model). Defer
    until first call so a server import that never evaluates a
    presidio-using policy doesn't pay the cost.
  - **Single shared engine.** Engine is thread-safe per Presidio
    docs and cheap to call repeatedly. We cache it module-locally.
  - **Conservative entity types.** PERSON, EMAIL_ADDRESS, PHONE_NUMBER,
    US_SSN, CREDIT_CARD, IP_ADDRESS, US_PASSPORT, US_DRIVER_LICENSE.
    We deliberately skip URL, DATE_TIME, NRP — those have high false-
    positive rates on agent traffic (most agent outputs include URLs
    and dates legitimately).
  - **Errors swallowed.** Bad input or analyzer failure → 0.0 / [].
    The detector must never raise into the policy engine.

Public API:

  - ``available: bool`` — module-level flag, False when import failed.
  - ``presidio_pii_score(text) -> float`` — max confidence across
    detected entities, 0.0 when none / empty / unavailable.
  - ``presidio_pii_entities(text) -> list[dict]`` — entities with
    ``{entity_type, score, start, end}`` for the dashboard view.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

logger = logging.getLogger("lumin.api.firewall.detectors.presidio")


# ---------------------------------------------------------------------------
# Optional import — module compiles even when presidio isn't installed.
# ---------------------------------------------------------------------------

try:
    from presidio_analyzer import AnalyzerEngine  # type: ignore
    available = True
except Exception:  # pragma: no cover — exercised at install time
    AnalyzerEngine = None  # type: ignore
    available = False


# Allow- and skip-lists. The allow list is what we expose; anything
# returned by Presidio that isn't in here is dropped before the score
# is computed.
_ALLOWED_ENTITIES = frozenset({
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
})


# Lazy-init machinery. We only construct the AnalyzerEngine on first
# call because instantiation loads spaCy models and is slow.
_engine: Optional[Any] = None
_engine_lock = threading.Lock()
_engine_init_failed = False


def _get_engine() -> Optional[Any]:
    """Return the singleton AnalyzerEngine, instantiating it on first
    call. Returns None when presidio isn't installed or init fails.
    """
    global _engine, _engine_init_failed
    if not available:
        return None
    if _engine_init_failed:
        return None
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_init_failed:
            return None
        try:
            _engine = AnalyzerEngine()
        except Exception:
            # Most likely cause: missing spaCy model. Mark init as
            # failed so subsequent calls don't keep retrying — they
            # all fall back to safe defaults. Rule 7.
            logger.warning(
                "presidio AnalyzerEngine init failed — semantic PII "
                "detection disabled for this process",
                exc_info=True,
            )
            _engine_init_failed = True
            _engine = None
    return _engine


def presidio_pii_score(text: Optional[str]) -> float:
    """Max-confidence score (0.0 - 1.0) across detected PII entities.

    Returns 0.0 when:
      - text is None or empty
      - presidio is not installed (``available == False``)
      - the analyzer engine fails to initialize
      - the analyze call raises
      - no entity in the allow-list is detected
    """
    if not text or not isinstance(text, str):
        return 0.0
    engine = _get_engine()
    if engine is None:
        return 0.0
    try:
        results = engine.analyze(
            text=text,
            language="en",
            entities=list(_ALLOWED_ENTITIES),
        )
    except Exception:
        logger.debug("presidio analyze failed", exc_info=True)
        return 0.0
    if not results:
        return 0.0
    try:
        scores = [
            float(getattr(r, "score", 0.0))
            for r in results
            if getattr(r, "entity_type", None) in _ALLOWED_ENTITIES
        ]
    except Exception:
        return 0.0
    if not scores:
        return 0.0
    return max(scores)


def presidio_pii_entities(text: Optional[str]) -> List[dict]:
    """List of detected entities, each ``{entity_type, score, start,
    end}``. Used by the dashboard's "what was detected" panel.

    Returns ``[]`` on the same conditions ``presidio_pii_score`` returns
    0.0 (no input / not installed / init failed / engine error).
    """
    if not text or not isinstance(text, str):
        return []
    engine = _get_engine()
    if engine is None:
        return []
    try:
        results = engine.analyze(
            text=text,
            language="en",
            entities=list(_ALLOWED_ENTITIES),
        )
    except Exception:
        logger.debug("presidio analyze failed", exc_info=True)
        return []
    out: List[dict] = []
    try:
        for r in results:
            etype = getattr(r, "entity_type", None)
            if etype not in _ALLOWED_ENTITIES:
                continue
            out.append({
                "entity_type": etype,
                "score": float(getattr(r, "score", 0.0)),
                "start": int(getattr(r, "start", 0)),
                "end": int(getattr(r, "end", 0)),
            })
    except Exception:
        return []
    return out


def _reset_engine_for_tests() -> None:
    """Drop the cached engine and any init-failure flag. Used by the
    test suite to exercise the lazy-init path repeatedly."""
    global _engine, _engine_init_failed
    with _engine_lock:
        _engine = None
        _engine_init_failed = False
