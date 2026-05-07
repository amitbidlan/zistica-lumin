"""Tests for the synthetic adversarial generator — Slice 3 PR O / §14.2."""

from __future__ import annotations

import pytest

from firewall import attack_generator as gen


def test_generate_returns_attacks_for_default_categories() -> None:
    # Bump the limit past the default 100 so all categories surface.
    out = gen.generate_attacks(limit=10_000)
    assert len(out) > 0
    cats = {a["category"] for a in out}
    expected = set(gen.available_categories())
    assert cats == expected


def test_generate_filters_by_category() -> None:
    out = gen.generate_attacks(categories=["destructive_shell"])
    assert all(a["category"] == "destructive_shell" for a in out)
    # All shell attacks land at before_tool_call.
    assert all(a["lifecycle"] == "before_tool_call" for a in out)
    assert all(a["tool_name"] == "shell" for a in out)


def test_generate_unknown_category_skips() -> None:
    """Unknown category logs a warning and skips, doesn't crash."""
    out = gen.generate_attacks(categories=["nonexistent"])
    assert out == []


def test_generate_respects_limit() -> None:
    out = gen.generate_attacks(limit=3)
    assert len(out) == 3


def test_prompt_injection_attacks_have_messages() -> None:
    out = gen.generate_attacks(categories=["prompt_injection"])
    assert len(out) > 0
    for a in out:
        assert "messages" in a
        assert a["messages"][0]["role"] == "user"
        assert "block" in a["expected_to"]


def test_seed_prompt_prepended() -> None:
    """When seed_prompt is set, prompt-injection attacks have the
    seed text BEFORE the attack — closer to real-world placement."""
    out = gen.generate_attacks(
        categories=["prompt_injection"],
        seed_prompt="What's the weather in Paris?",
        limit=3,
    )
    for a in out:
        content = a["messages"][0]["content"]
        assert content.startswith("What's the weather in Paris?")


def test_seed_ask_replaces_default() -> None:
    """seed_ask overrides the rotating default ``{ask}`` substitution."""
    out = gen.generate_attacks(
        categories=["prompt_injection"],
        seed_ask="reveal the API key",
        limit=10,
    )
    # All asks should match.
    asks = {a.get("ask") for a in out}
    assert asks == {"reveal the API key"}


def test_destructive_shell_attacks_have_command_params() -> None:
    out = gen.generate_attacks(categories=["destructive_shell"])
    assert len(out) > 0
    for a in out:
        assert "params" in a
        assert "command" in a["params"]
        assert "block" in a["expected_to"]


def test_ssrf_attacks_target_internal_urls() -> None:
    out = gen.generate_attacks(categories=["ssrf"])
    assert len(out) > 0
    for a in out:
        assert a["tool_name"] == "web_fetch"
        url = a["params"]["url"]
        # Quick sanity — the URL is recognizably internal.
        assert any(
            tok in url
            for tok in ("169.254", "metadata", "localhost", "127.0", "10.0", "file://", "[::1]", "gopher://")
        )


def test_path_traversal_attacks_have_path_param() -> None:
    out = gen.generate_attacks(categories=["path_traversal"])
    assert len(out) > 0
    for a in out:
        assert "path" in a["params"]


def test_exfiltration_attacks_target_output() -> None:
    """Exfil patterns appear in MODEL OUTPUT, so the lifecycle is
    after_proxy_call and the payload is on ``output`` not ``messages``."""
    out = gen.generate_attacks(categories=["exfiltration"])
    assert len(out) > 0
    for a in out:
        assert a["lifecycle"] == "after_proxy_call"
        assert "output" in a
        assert "messages" not in a


def test_ipi_attacks_target_tool_output() -> None:
    """IPI lives in tool output → after_tool_call."""
    out = gen.generate_attacks(categories=["ipi"])
    assert len(out) > 0
    for a in out:
        assert a["lifecycle"] == "after_tool_call"
        assert "output" in a


def test_jailbreak_attacks_have_canonical_shapes() -> None:
    """Each jailbreak template is generated; verify a couple
    well-known ones make it into the output."""
    out = gen.generate_attacks(categories=["jailbreak"])
    names = {a["name"] for a in out}
    assert "dan_classic" in names
    assert "researcher_persona" in names


def test_sensitive_file_attacks() -> None:
    out = gen.generate_attacks(categories=["sensitive_file"])
    assert len(out) > 0
    for a in out:
        cmd = a["params"]["command"]
        assert any(
            tok in cmd
            for tok in ("/etc/passwd", "/etc/shadow", ".ssh/", ".aws/credentials", ".kube/config", ".env", "auth.log")
        )


def test_available_categories_complete() -> None:
    """The list includes all known categories."""
    cats = gen.available_categories()
    expected = {
        "prompt_injection",
        "jailbreak",
        "exfiltration",
        "destructive_shell",
        "sensitive_file",
        "ssrf",
        "path_traversal",
        "ipi",
    }
    assert set(cats) == expected
