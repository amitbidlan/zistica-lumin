"""Regex-based detection — §6.1 of AGENT_FIREWALL_SPEC.md.

Pure-Python module with no optional deps. Always available. Used both
as the first-line detector in the engine pipeline (cheap, deterministic,
runs in <2ms on 10KB inputs) and as the source of `looks_like_secret`,
`is_destructive_path`, etc. builtins exposed in policy expressions
(see ``firewall.builtins``).

Pattern philosophy:

  - Shape-based, not semantic. Catches "this looks like an SSN" but
    not "this is a name." Semantic PII goes through Microsoft Presidio
    in a later slice.
  - Curated, not exhaustive. Each pattern has been chosen because it
    has a low false-positive rate on observed agent traffic. Over-
    matching breaks the trust contract — the bypass log (§14.4) will
    surface near-misses for tuning, but the default set should never
    fire on legitimate agent payloads.
  - Versioned. The ``PATTERNS`` dict is the API contract; tests assert
    every pattern compiles and matches at least one canonical
    positive sample.

Performance: every pattern compiles at import time. Lookups are
``re.search``, so the cost is bounded by the pattern complexity, not
the input size to first-match. We run all patterns in sequence rather
than as one giant alternation because failed patterns short-circuit
early and operator-friendly error messages matter more than peak
throughput.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern, Tuple

# ---------------------------------------------------------------------------
# Curated patterns. KEEP THIS LIST SMALL.
#
# Each entry: (name, compiled regex, brief description). The name is the
# stable identifier policies use, e.g.
#
#     condition: looks_like_secret(output)
#
# is implemented by iterating over (kind in ``SECRET_KINDS``) and asking
# ``PATTERNS[kind].search(output)``. Adding a new pattern means: appending
# here, adding a positive sample to the test suite, and (if it's a secret
# shape) appending its name to ``SECRET_KINDS`` below.
# ---------------------------------------------------------------------------

PATTERNS: Dict[str, Pattern[str]] = {
    # --- Cloud-provider credentials ---
    "aws_access_key_id": re.compile(r"\b(AKIA|ASIA|AROA)[0-9A-Z]{16}\b"),
    # AWS secret access key — 40 chars of base64 alphabet. We require
    # surrounding boundary because just "any 40 chars of base64" is too
    # noisy. The entropy check (computed by callers) discriminates real
    # secrets from random text that happens to match the shape.
    "aws_secret_access_key_shape": re.compile(
        r"(?<![A-Za-z0-9/+])([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])"
    ),
    "gcp_api_key": re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    "azure_storage_key": re.compile(
        r"\b[A-Za-z0-9+/]{86}==\b"
    ),

    # --- Source-control PATs ---
    # GitHub: ghp_, gho_, ghu_, ghr_, ghs_ — 36+ chars after the prefix.
    "github_pat": re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{36,}\b"),
    "gitlab_pat": re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),

    # --- API keys (generic) ---
    # Common: stripe_live_, sk_live_, sk-, etc. Catch the obvious ones
    # without trying to enumerate every vendor.
    "stripe_secret_key": re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{24,}\b"),
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "anthropic_api_key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    # Last-resort generic: ``api_key=`` followed by a longish token.
    # Bounded by punctuation/whitespace to keep false positives low.
    "generic_api_key": re.compile(
        r'(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?token)["\s:=]+'
        r'["\']?([A-Za-z0-9_\-]{20,})["\']?'
    ),

    # --- Tokens / JWTs ---
    # JWT: 3 dot-separated base64url segments. The header almost always
    # starts with eyJ (base64 of {"). Tightening to that prefix cuts
    # false positives on random hex-y data.
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),

    # --- US PII shapes (low-FP) ---
    "us_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Credit card *shape* — 13-19 digits with optional dashes/spaces in
    # 4-digit groups. The Luhn check (luhn_valid below) eliminates
    # ~90% of false positives like phone numbers and order IDs.
    "credit_card_shape": re.compile(
        r"\b(?:\d[ -]*?){13,19}\b"
    ),
    # NA phone: optional country code, optional separators, NANP rules.
    "phone_nanp": re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?[2-9]\d{2}[-.\s]?\d{4}\b"
    ),
    "email": re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
    ),
    "us_drivers_license_partial": re.compile(
        # Heuristic — many states use a letter prefix + 7 digits.
        r"\b[A-Z]\d{7,8}\b"
    ),

    # --- Network identifiers (private/internal) ---
    "ipv4": re.compile(
        r"\b(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){3}\b"
    ),
    "private_ipv4": re.compile(
        # RFC1918 + link-local. Useful for SSRF detection on tool args.
        r"\b(?:10\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){2}"
        r"|172\.(?:1[6-9]|2\d|3[01])(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){2}"
        r"|192\.168(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){2}"
        r"|127\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){2}"
        r"|169\.254(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){2})\b"
    ),

    # --- Agent-specific exfil shapes ---
    # Image-markdown exfil: ``![alt](https://attacker.tld/?data=...)``.
    # Found in CVE-2024-38206 (ASCII smuggling) family and Bing/Copilot
    # exfil writeups. Look for image markdown with a URL containing a
    # querystring — the legitimate use of this shape is rare in agent
    # outputs (most agents produce links with text, not images).
    "image_markdown_exfil": re.compile(
        r"!\[[^\]]*\]\((https?://[^)]+\?[^)]+)\)"
    ),
    # Hidden-character smuggling: Unicode "tag" characters (U+E0000 to
    # U+E007F) and other invisible characters used to hide instructions
    # inside otherwise-normal-looking text. Catching even one of these
    # in agent traffic is an immediate suspicion signal.
    "ascii_smuggling": re.compile(r"[\U000E0000-\U000E007F​-‏‪-‮⁦-⁩]"),
}


# Subset of patterns that are "secrets" — used by ``looks_like_secret``
# to scan a string for any of them. SSN/CC/phone/email are PII, not
# secrets, and live in their own builtins.
SECRET_KINDS: Tuple[str, ...] = (
    "aws_access_key_id",
    "aws_secret_access_key_shape",
    "gcp_api_key",
    "azure_storage_key",
    "github_pat",
    "gitlab_pat",
    "stripe_secret_key",
    "openai_api_key",
    "anthropic_api_key",
    "generic_api_key",
    "jwt",
)


# Subset of patterns that are "PII shapes" — used by ``regex_pii_scan``.
PII_KINDS: Tuple[str, ...] = (
    "us_ssn",
    "credit_card_shape",  # confirmed by luhn_valid before flagging
    "phone_nanp",
    "email",
    "us_drivers_license_partial",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def luhn_valid(digits: str) -> bool:
    """Mod-10 (Luhn) check on a credit-card-shape string.

    Strips non-digit characters, requires 13-19 digits, returns False
    for clearly bogus shapes (all zeros, wrong length). This is the
    discriminator that separates real card numbers from phone numbers
    that happen to match the digit-count shape.
    """
    only_digits = "".join(c for c in digits if c.isdigit())
    if not (13 <= len(only_digits) <= 19):
        return False
    if all(d == "0" for d in only_digits):
        return False
    total = 0
    parity = len(only_digits) % 2
    for i, d in enumerate(only_digits):
        n = int(d)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def shannon_entropy(s: str) -> float:
    """Bits per character — used to discriminate "looks like a secret"
    from "random words that match the secret length."

    A typical English sentence has ~4 bits/char; a base64-encoded
    secret is ~5.5-6 bits/char. The discriminator threshold for
    ``looks_like_secret`` is 4.5 — empirically, real secrets land
    above, prose lands below.
    """
    if not s:
        return 0.0
    from math import log2
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(s))
    return -sum((c / n) * log2(c / n) for c in counts.values())


# ---------------------------------------------------------------------------
# Public scan helpers — used directly from policy builtins
# ---------------------------------------------------------------------------


def regex_match(s: Optional[str], pattern: str) -> bool:
    """Boolean match — exposed to policy expressions as the
    ``regex_match`` builtin. Returns False (never raises) on bad
    inputs; Rule 7."""
    if not s:
        return False
    try:
        return re.search(pattern, s) is not None
    except re.error:
        return False


def regex_extract(s: Optional[str], pattern: str) -> Optional[str]:
    """Return the first regex match (group 1 if grouping present, else
    group 0), or None. Like ``regex_match`` but returns the matched
    text — useful for rewrite actions and decision explanations."""
    if not s:
        return None
    try:
        m = re.search(pattern, s)
        if not m:
            return None
        if m.groups():
            return m.group(1)
        return m.group(0)
    except re.error:
        return None


def secrets_in(s: Optional[str]) -> List[Tuple[str, str]]:
    """List of (kind, matched_text) pairs for every secret-shape
    pattern that matches in ``s``. Empty list = no secrets found.

    Generic API key shape matches go through an entropy check to
    cut false positives on "api_key=hello world" style placeholder
    text — the matched token must have entropy > 3.5 (somewhere
    between English prose and base64).
    """
    if not s:
        return []
    results: List[Tuple[str, str]] = []
    for kind in SECRET_KINDS:
        m = PATTERNS[kind].search(s)
        if not m:
            continue
        if kind == "aws_secret_access_key_shape":
            # 40-char base64 has many false positives. Apply entropy.
            tok = m.group(1)
            if shannon_entropy(tok) < 4.5:
                continue
            results.append((kind, tok))
        elif kind == "generic_api_key":
            tok = m.group(1) if m.groups() else m.group(0)
            if shannon_entropy(tok) < 3.5:
                continue
            results.append((kind, tok))
        else:
            results.append((kind, m.group(0)))
    return results


def looks_like_secret(s: Optional[str]) -> bool:
    """Convenience boolean wrapper around ``secrets_in``."""
    return bool(secrets_in(s))


def regex_pii_scan(s: Optional[str]) -> List[Tuple[str, str]]:
    """List of (kind, matched_text) PII shape matches. Credit-card
    matches are filtered through Luhn before being included; non-
    Luhn matches are dropped (they're phone numbers or order IDs,
    not real card numbers)."""
    if not s:
        return []
    results: List[Tuple[str, str]] = []
    for kind in PII_KINDS:
        for m in PATTERNS[kind].finditer(s):
            text = m.group(0)
            if kind == "credit_card_shape":
                if not luhn_valid(text):
                    continue
            results.append((kind, text))
    return results


def has_pii(s: Optional[str]) -> bool:
    """Convenience boolean wrapper."""
    return bool(regex_pii_scan(s))


def has_image_markdown_exfil(s: Optional[str]) -> bool:
    """True when ``s`` contains an image-markdown URL with a query
    string — the canonical exfil shape. Cheap, high-signal, ships
    enabled by default."""
    if not s:
        return False
    return PATTERNS["image_markdown_exfil"].search(s) is not None


def has_ascii_smuggling(s: Optional[str]) -> bool:
    """True if any Unicode tag character or bidirectional override
    appears in ``s``. These have no legitimate use in agent output
    and are the canonical hidden-instruction smuggling shape."""
    if not s:
        return False
    return PATTERNS["ascii_smuggling"].search(s) is not None
