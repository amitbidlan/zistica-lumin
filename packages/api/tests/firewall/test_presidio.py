"""Tests for the Presidio detector — Tier 2.1.

Most tests are skipped when presidio isn't installed; the always-passing
``test_unavailable_returns_zero`` exercises the graceful-degradation
path that matters when operators don't opt in to the heavier ML deps.
"""

from __future__ import annotations

import pytest

from firewall.detectors import presidio


pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.mark.skipif(
    not presidio.available,
    reason="presidio-analyzer not installed in this environment",
)
def test_score_for_personal_address_string() -> None:
    """A canonical PII-laden string should clear the 0.5 threshold —
    Presidio detects PERSON + (likely) US locations."""
    score = presidio.presidio_pii_score(
        "John Smith lives in 123 Main St, Springfield"
    )
    assert score > 0.5, f"expected score > 0.5 for PII string, got {score}"


@pytest.mark.skipif(
    not presidio.available,
    reason="presidio-analyzer not installed in this environment",
)
def test_score_for_neutral_text_is_zero() -> None:
    """Plain prose with no PII should not register any allow-listed
    entity — score is 0.0 (no detections from the curated set)."""
    score = presidio.presidio_pii_score("this is just text")
    assert score == 0.0


def test_score_for_none_or_empty() -> None:
    """None / empty / non-str input → 0.0 unconditionally; this path
    runs without requiring presidio to be installed."""
    assert presidio.presidio_pii_score(None) == 0.0
    assert presidio.presidio_pii_score("") == 0.0
    assert presidio.presidio_pii_score(123) == 0.0  # type: ignore[arg-type]


@pytest.mark.skipif(
    not presidio.available,
    reason="presidio-analyzer not installed in this environment",
)
def test_entities_for_ssn_string_includes_us_ssn() -> None:
    """An SSN-laden string should produce at least one US_SSN entity in
    the structured output the dashboard reads. Use a recognized SSN
    shape — Presidio's recognizer rejects 9-digit groups whose area
    number violates SSA allocation rules (so "123-45-6789" is dropped
    despite matching the visual pattern)."""
    entities = presidio.presidio_pii_entities(
        "Patient SSN is 451-66-2342, please file"
    )
    assert any(e["entity_type"] == "US_SSN" for e in entities), (
        f"expected US_SSN in entities, got {entities}"
    )


@pytest.mark.skipif(
    not presidio.available,
    reason="presidio-analyzer not installed in this environment",
)
def test_entities_for_empty_input_is_empty_list() -> None:
    assert presidio.presidio_pii_entities("") == []
    assert presidio.presidio_pii_entities(None) == []


def test_unavailable_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """When presidio's import is simulated as failed, score is always
    0.0 and entities is always [] — the documented fallback. Always
    runs, regardless of whether presidio is actually installed."""
    monkeypatch.setattr(presidio, "available", False)
    presidio._reset_engine_for_tests()
    try:
        assert presidio.presidio_pii_score(
            "John Smith lives in 123 Main St"
        ) == 0.0
        assert presidio.presidio_pii_entities(
            "Patient SSN is 123-45-6789"
        ) == []
    finally:
        presidio._reset_engine_for_tests()


def test_score_swallows_engine_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AnalyzerEngine() raises (e.g. spaCy model missing), the
    detector falls back to 0.0 / [] without bubbling the exception
    up into the policy engine. Rule 7."""
    presidio._reset_engine_for_tests()

    class _BoomEngine:
        def __init__(self, *a, **kw):
            raise RuntimeError("simulated init failure")

    # Force the lazy-init code path even if presidio isn't installed.
    monkeypatch.setattr(presidio, "available", True)
    monkeypatch.setattr(presidio, "AnalyzerEngine", _BoomEngine)
    try:
        assert presidio.presidio_pii_score("John Smith") == 0.0
        assert presidio.presidio_pii_entities("John Smith") == []
    finally:
        presidio._reset_engine_for_tests()
