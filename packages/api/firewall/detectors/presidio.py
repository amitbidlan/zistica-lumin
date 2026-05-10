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
import os
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
#
# 2026-05-10 — added LOCATION to catch geographic identifiers in
# customer-support / sales contexts (city / country / region names
# that uniquely identify a tenant deal). Did NOT add ORGANIZATION:
# Presidio doesn't ship a default ORG recognizer (spaCy NER emits ORG
# but Presidio doesn't map it). Adding ORG requires a custom
# recognizer (PatternRecognizer or registered SpacyRecognizer) —
# tracked as a follow-up.
_ALLOWED_ENTITIES = frozenset({
    "PERSON",
    "LOCATION",
    "ORGANIZATION",
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
            # Allow operator to pick a smaller spaCy model via env var
            # (saves ~700MB image size at modest accuracy cost). When
            # unset, falls through to Presidio's default behaviour
            # which uses ``en_core_web_lg``. The Dockerfile sets this
            # env var to whatever was downloaded at build time, so
            # the model name and the container env stay aligned.
            _model_name = os.environ.get("LUMIN_PRESIDIO_MODEL")
            if _model_name and _model_name != "en_core_web_lg":
                from presidio_analyzer.nlp_engine import NlpEngineProvider  # type: ignore
                provider = NlpEngineProvider(nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": _model_name}],
                })
                _engine = AnalyzerEngine(nlp_engine=provider.create_engine())
            else:
                _engine = AnalyzerEngine()
            # Register a custom ORGANIZATION recognizer. Presidio
            # ships PERSON / LOCATION / EMAIL_ADDRESS / etc. but no
            # default org recognizer — yet enterprise customer names
            # (Northstar Logistics, GE Capital, Meridian Health
            # Systems) are exactly the cross-tenant signal we need
            # to redact. PatternRecognizer with a corporate-suffix
            # regex catches the common enterprise shape (suffix-
            # bearing names) without needing a custom NER model.
            # 2026-05-10 — added after live test showed Northstar /
            # Meridian / Acme / GE Capital all passed L3 unredacted.
            try:
                from presidio_analyzer import PatternRecognizer, Pattern  # type: ignore
                # Capital-anchored: each prefix word MUST start with
                # uppercase letter, suffix MUST be a known company
                # token. Requires at least 1 prefix word so the
                # suffix alone (e.g. "Ltd", "Health") doesn't fire
                # an ORG hit in isolation.
                #
                # CRITICAL: Presidio's PatternRecognizer hardcodes
                # ``re.IGNORECASE`` on every Pattern. Without the
                # ``(?-i:...)`` inline flag, the prefix-word
                # ``[A-Z]`` constraint is silently ignored and the
                # regex matches lowercase phrases too — e.g.
                # "am working with Bobcorp Industries Ltd" got
                # matched as ORG because "am working with" all
                # passed `[A-Z]` once IGNORECASE was applied. The
                # `(?-i:[A-Z])` forces case-sensitivity locally
                # while leaving the suffix-token group case-
                # insensitive (so "ltd"/"LTD" still match suffixes).
                org_pattern = Pattern(
                    name="org_with_suffix",
                    regex=(
                        r"(?:(?<=^)|(?<=[\s,;:\-\(\[]))"
                        r"(?-i:(?:[A-Z][A-Za-z0-9&.,'\-]{1,32}\s+){1,4})"
                        r"(?:Inc\.?|Incorporated|Corp\.?|Corporation|"
                        r"LLC|L\.L\.C\.?|Ltd\.?|Limited|"
                        r"GmbH|AG|"
                        r"Company|"
                        r"Group|Holdings|Holding|"
                        r"Capital|Partners|Ventures|"
                        r"Logistics|Shipping|Freight|"
                        r"Health|Healthcare|Medical|Pharma|Pharmaceuticals|"
                        r"Systems|Solutions|Services|Industries|"
                        r"Technologies|Technology|Software|"
                        r"Bank|Banking|Insurance|Financial|"
                        r"Energy|Power|Petroleum|Resources|"
                        r"Telecom|Communications|Networks|"
                        r"Enterprise|Enterprises|Consulting|Advisors)"
                        r"\b"
                    ),
                    score=0.7,
                )
                org_recognizer = PatternRecognizer(
                    supported_entity="ORGANIZATION",
                    patterns=[org_pattern],
                    supported_language="en",
                )
                _engine.registry.add_recognizer(org_recognizer)
            except Exception:
                logger.warning(
                    "presidio: failed to register ORGANIZATION recognizer "
                    "— enterprise customer names will pass L3 unredacted",
                    exc_info=True,
                )
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
    end, text}``. Used by the dashboard's "what was detected" panel
    AND by ``vault._extract_facts`` (which reads ``text`` to build
    the foreign-secret excerpt list for L3 redaction).

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
            start = int(getattr(r, "start", 0))
            end = int(getattr(r, "end", 0))
            out.append({
                "entity_type": etype,
                "score": float(getattr(r, "score", 0.0)),
                "start": start,
                "end": end,
                # The actual entity substring. Without this,
                # ``vault._extract_facts`` (which keys on ``text``)
                # silently drops every Presidio hit — historically the
                # reason names + orgs slipped through L3 even with
                # Presidio installed. (Fixed 2026-05-10 alongside the
                # Presidio enable in the Docker image.)
                "text": text[start:end],
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
