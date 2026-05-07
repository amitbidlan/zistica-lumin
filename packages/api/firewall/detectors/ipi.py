"""IPI sniffer — Slice 3 PR L / spec §6.9.

Indirect prompt injection (IPI): an attacker plants instructions inside
data the agent retrieves — search results, scraped pages, support
ticket bodies, RAG-context documents — so the model treats them as
its own instructions on the next turn. Direct injection lives in
the user message; IPI lives in everything else.

The dangerous part: by the time tool output reaches the model, the
agent's already retrieved it. The firewall has one shot to spot it
before the model sees it. That happens at ``after_tool_call``.

This detector wraps Prompt Guard 2 with three IPI-specific
preprocessors:

  1. **HTML stripping** — IPI hides in attributes (``<img alt="ignore
     all previous instructions and ...">``), comments, and obscure
     tags. Strip first, classify the visible text + the suspicious
     attributes separately.

  2. **ASCII / Unicode smuggling** — invisible chars (zero-width
     space, BiDi overrides, Unicode tag chars per CVE-2024-38206) are
     used to hide instructions inside otherwise-benign-looking text.
     Flagged as high-confidence injection regardless of model score.

  3. **Multi-passage scoring** — when the input is a list of search
     results / RAG chunks (sniffed by JSON list shape or simple
     newline heuristic), each passage is scored separately and the
     max is returned. A 0.95-injection chunk hidden in 9 benign ones
     averages to 0.1 if you classify the concatenation; the max
     surfaces it.

When Prompt Guard 2 isn't installed (the optional dep), the detector
falls back to: regex_pack's ``has_ascii_smuggling`` + a small set of
literal IPI markers ("ignore previous instructions", "system:" at
the start of a tool result, etc.). Returns 0.0 if neither path
yields a hit — Rule 7.

Public API:

  ``ipi_score(text) -> float`` — 0.0-1.0, max over passages
  ``ipi_unsafe(text) -> bool`` — score > 0.7 (Meta's threshold)
  ``ipi_passages(text) -> List[dict]`` — per-passage breakdown
                                          for the dashboard
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from firewall.detectors import prompt_guard as pg_det
from firewall.detectors import regex_pack as rp

logger = logging.getLogger("lumin.api.firewall.detectors.ipi")


# Threshold above which we call something "unsafe". Mirrors the
# Prompt Guard 2 default; tunable per-rule via the score builtin.
DEFAULT_THRESHOLD = 0.7


# Always-on IPI markers — we look for these even when Prompt Guard
# 2 isn't installed. Keep the list short and high-precision; the
# point is "the regex catches the obvious cases when ML can't".
_IPI_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore the above",
    "disregard all previous",
    "you are now",
    "new instructions:",
    "system:",
    "<|system|>",
    "<|im_start|>system",
)


# The detector is "available" whenever it can do *something* —
# prompt_guard available means richer signal, but the ASCII +
# regex paths run unconditionally.
available = True


# HTML strip — leaves attribute values + visible text, drops tag
# brackets. We don't use a real HTML parser because (a) tool output
# is often broken HTML, (b) we want to *see* the attributes, not
# erase them. Regex is the right tool.
_TAG_RE = re.compile(r"<[^>]*>")
_ATTR_RE = re.compile(r'\b(alt|title|aria-label|data-[a-z-]+)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def _extract_html_payloads(s: str) -> List[str]:
    """Return text fragments worth scoring separately from a string
    that may contain HTML. Returns the visible text + each suspicious
    attribute value + each comment body. Operators wanting attribute-
    only scoring can still write rules against the second-pass output."""
    payloads: List[str] = []
    # Comments first — they survive HTML rendering invisibly.
    for m in _HTML_COMMENT_RE.finditer(s):
        payloads.append(m.group(1).strip())
    # Common attributes that hide IPI.
    for m in _ATTR_RE.finditer(s):
        payloads.append(m.group(2).strip())
    # The visible-text pass — strip tags + comments and keep what's left.
    visible = _HTML_COMMENT_RE.sub(" ", s)
    visible = _TAG_RE.sub(" ", visible)
    visible = re.sub(r"\s+", " ", visible).strip()
    if visible:
        payloads.append(visible)
    return payloads


def _split_passages(s: str) -> List[str]:
    """Split a tool result into passages for multi-passage scoring.

    Three sniffers, in order:

      1. JSON array of strings or objects — parse + extract text fields
      2. Markdown-style numbered list ("1. ..." / "2. ...")
      3. Triple-newline-separated chunks (search-result style)

    Falls back to ``[s]`` (single passage) if none of the above match.
    Cap at 50 passages — past that we're scoring a haystack, and
    Prompt Guard 2 inference cost grows linearly.
    """
    if not s or len(s) < 50:
        return [s] if s else []

    # JSON array path — common shape for OpenClaw web search results,
    # tavily, brave_search, etc.
    s_stripped = s.strip()
    if s_stripped.startswith("[") and s_stripped.endswith("]"):
        try:
            data = json.loads(s_stripped)
            if isinstance(data, list):
                out: List[str] = []
                for item in data[:50]:
                    if isinstance(item, str):
                        out.append(item)
                    elif isinstance(item, dict):
                        # Common text fields across search APIs.
                        for k in ("text", "content", "snippet", "body", "description", "title"):
                            v = item.get(k)
                            if isinstance(v, str) and v:
                                out.append(v)
                if out:
                    return out
        except (ValueError, TypeError):
            pass

    # Numbered-list path
    numbered = re.split(r"\n\s*\d+[\.\)]\s+", s)
    if len(numbered) >= 3:
        return [p.strip() for p in numbered if p.strip()][:50]

    # Triple-newline path (between search results)
    if "\n\n\n" in s:
        chunks = [c.strip() for c in s.split("\n\n\n") if c.strip()]
        if len(chunks) >= 2:
            return chunks[:50]

    # Fallback — score as one passage.
    return [s]


def _heuristic_score(text: str) -> float:
    """Always-on regex / Unicode heuristics. Returns 0.0-1.0.

    Any single marker is a high-confidence hit (1.0); ASCII smuggling
    is also 1.0 because there's no benign reason to find Unicode tag
    chars in agent traffic. Both miss → 0.0.
    """
    if not text:
        return 0.0
    lower = text.lower()
    for marker in _IPI_MARKERS:
        if marker in lower:
            return 1.0
    if rp.has_ascii_smuggling(text):
        return 1.0
    return 0.0


def _classify_one(text: str) -> float:
    """Score a single passage. Combines heuristic + Prompt Guard 2.
    Returns the max — heuristic is a sharp 0/1 floor; Prompt Guard
    fills in the smooth middle."""
    if not text:
        return 0.0
    h = _heuristic_score(text)
    if h >= 1.0:
        # Heuristic short-circuit — no need to pay for ML inference.
        return 1.0
    pg = pg_det.prompt_guard_score(text) if pg_det.available else 0.0
    return max(h, pg)


# ---- public API -----------------------------------------------------------


def ipi_score(text: Optional[str]) -> float:
    """Return the maximum IPI-injection confidence across all
    extracted passages of ``text``. 0.0 on falsy / non-str input
    (Rule 7)."""
    if not text or not isinstance(text, str):
        return 0.0
    try:
        # First, decompose HTML if present.
        if "<" in text and ">" in text:
            payloads = _extract_html_payloads(text)
        else:
            payloads = [text]

        # Then split each payload into passages.
        all_passages: List[str] = []
        for p in payloads:
            all_passages.extend(_split_passages(p))

        if not all_passages:
            return 0.0

        # Score each, return the max.
        best = 0.0
        for passage in all_passages:
            score = _classify_one(passage)
            if score > best:
                best = score
                if best >= 1.0:
                    break  # short-circuit: can't get higher
        return best
    except Exception:
        logger.exception("ipi: scoring failed")
        return 0.0


def ipi_unsafe(text: Optional[str], threshold: float = DEFAULT_THRESHOLD) -> bool:
    """True iff IPI score for ``text`` exceeds ``threshold`` (default 0.7)."""
    return ipi_score(text) >= threshold


def ipi_passages(text: Optional[str]) -> List[Dict[str, Any]]:
    """Per-passage breakdown for the dashboard. Returns a list of
    ``{"passage": str, "score": float, "marker": Optional[str]}``
    dicts so operators can see which passage tripped the rule.

    Empty list on falsy input or scoring failure (Rule 7)."""
    if not text or not isinstance(text, str):
        return []
    try:
        if "<" in text and ">" in text:
            payloads = _extract_html_payloads(text)
        else:
            payloads = [text]

        all_passages: List[str] = []
        for p in payloads:
            all_passages.extend(_split_passages(p))

        out: List[Dict[str, Any]] = []
        for passage in all_passages:
            score = _classify_one(passage)
            marker = None
            lower = passage.lower()
            for m in _IPI_MARKERS:
                if m in lower:
                    marker = m
                    break
            if marker is None and rp.has_ascii_smuggling(passage):
                marker = "ascii_smuggling"
            out.append({
                "passage": passage[:500],  # truncate so dashboard payloads stay small
                "score": score,
                "marker": marker,
            })
        return out
    except Exception:
        logger.exception("ipi: passages failed")
        return []
