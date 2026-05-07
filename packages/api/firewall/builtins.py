"""Policy-expression builtins — §3.5 + §6.2 of AGENT_FIREWALL_SPEC.md.

The functions in this module are exposed inside ``simpleeval`` policy
condition expressions. Two flavors:

  - **Stateless** (``regex_match``, ``url_host``, ``looks_like_secret``):
    pure functions, no DB access. Cheap. Always available.
  - **History-backed** (``session_total_tokens``, ``trace_total_cost``,
    ``tool_calls_in_trace``): require a DuckDB connection. Built as a
    factory that takes a ``Database`` and returns the bound builtins
    map. Cached for 1 second to avoid hammering DuckDB on bursty
    workloads.

Why a factory rather than a singleton?

  - Tests construct fresh ``Database(":memory:")`` per test; a global
    DB reference would point at a stale connection.
  - The decision endpoint will pass the request-scoped ``db`` into
    each evaluation, matching the existing ``policy_runtime.py``
    pattern.

Cache semantics: per-process, in-memory, 1-second TTL keyed by
``(function_name, *args)``. Tradeoff: a burst of 100 decisions in
the same second only hits DuckDB once per unique tuple. Stale data
within a single second is fine — these are aggregate metrics, not
authoritative state.

Rule 7: every builtin returns a safe default (False, 0, 0.0, None,
empty list) on error. A failed DuckDB query never crashes the engine.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from firewall.detectors import regex_pack as rp
from firewall.detectors import presidio as presidio_det
from firewall.detectors import prompt_guard as pg_det
from firewall.detectors import llama_guard as lg_det
from firewall.detectors import embedding as emb_det
from firewall.detectors import ipi as ipi_det
from firewall.detectors import llm_judge as judge_det

logger = logging.getLogger("lumin.api.firewall.builtins")


# ---------------------------------------------------------------------------
# Stateless builtins
# ---------------------------------------------------------------------------


def regex_match(s: Optional[str], pattern: str) -> bool:
    return rp.regex_match(s, pattern)


def regex_extract(s: Optional[str], pattern: str) -> Optional[str]:
    return rp.regex_extract(s, pattern)


def contains_any(s: Optional[str], needles) -> bool:
    """True if any of ``needles`` is a substring of ``s``. Tolerant of
    None / non-list inputs (Rule 7)."""
    if not s:
        return False
    if not needles:
        return False
    try:
        return any(str(n) in s for n in needles)
    except (TypeError, AttributeError):
        return False


def url_host(url: Optional[str]) -> str:
    """Hostname for a URL string. Empty string on parse failure or
    falsy input. Used by URL-allowlist policies."""
    if not url:
        return ""
    try:
        parsed = urlparse(str(url))
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def url_in_allowlist(url: Optional[str], allowlist) -> bool:
    """True if ``url_host(url)`` matches any host in the allowlist
    OR is a subdomain of one. Allowlist entries with leading dot
    (``.example.com``) are treated as "this domain and all
    subdomains"; entries without are exact-match.

    The subdomain semantics matter: ``api.acme.com`` should match
    an allowlist of ``[acme.com]`` only when the operator wants
    that — by default we keep it strict (exact). Operators who
    want wildcard subdomains write ``.acme.com``.
    """
    host = url_host(url)
    if not host:
        return False
    if not allowlist:
        return False
    try:
        for entry in allowlist:
            entry = str(entry).lower().strip()
            if not entry:
                continue
            if entry.startswith("."):
                # Subdomain wildcard. Strip leading dot, check suffix.
                bare = entry[1:]
                if host == bare or host.endswith("." + bare):
                    return True
            else:
                if host == entry:
                    return True
        return False
    except (TypeError, AttributeError):
        return False


def entropy(s: Optional[str]) -> float:
    """Shannon entropy bits/char. Used for secret detection."""
    if not s:
        return 0.0
    return rp.shannon_entropy(s)


def len_chars(s: Optional[str]) -> int:
    """``len()`` that's safe on None — returns 0."""
    if s is None:
        return 0
    try:
        return len(s)
    except TypeError:
        return 0


def looks_like_secret(s: Optional[str]) -> bool:
    return rp.looks_like_secret(s)


def has_pii(s: Optional[str]) -> bool:
    return rp.has_pii(s)


def presidio_pii_score(s: Optional[str]) -> float:
    """Semantic PII confidence score (0.0–1.0) via Microsoft Presidio.

    Returns 0.0 when presidio isn't installed — that's the expected
    state for operators who don't opt into the heavier ML deps.
    See ``firewall.detectors.presidio`` for the entity allow-list.
    """
    return presidio_det.presidio_pii_score(s)


def presidio_pii_entities(s: Optional[str]) -> list:
    """List of ``{entity_type, score, start, end}`` dicts for every
    PII entity Presidio detected. Empty list when presidio isn't
    installed or no entity matched."""
    return presidio_det.presidio_pii_entities(s)


def prompt_guard_score(s: Optional[str]) -> float:
    """Prompt-injection / jailbreak confidence score (0.0-1.0) via
    Meta's Prompt-Guard-2-22M classifier.

    Returns 0.0 when transformers/torch aren't installed — the
    expected state for operators staying on the regex-only path.
    See ``firewall.detectors.prompt_guard`` for label semantics
    and configuration.
    """
    return pg_det.prompt_guard_score(s)


def prompt_guard_label(s: Optional[str]) -> str:
    """Predicted label (BENIGN / INJECTION / JAILBREAK) for ``s``.
    Empty string when the detector isn't available."""
    return pg_det.prompt_guard_label(s)


def llama_guard_classify(s: Optional[str]) -> dict:
    """Classify ``s`` against the MLCommons hazard taxonomy via
    Llama Guard 4. Returns ``{"safe": bool, "categories": [..],
    "names": [..]}``.

    Returns the safe-by-default result when transformers/torch
    aren't installed — the expected state for operators staying
    on the regex / heuristic detection path.
    """
    return lg_det.llama_guard_classify(s)


def llama_guard_unsafe(s: Optional[str]) -> bool:
    """True iff Llama Guard 4 flagged ``s`` as unsafe in any
    hazard category. Convenience wrapper for ``action: block`` rules.
    Always False when the detector isn't available."""
    return lg_det.llama_guard_unsafe(s)


def llama_guard_categories(s: Optional[str]) -> list:
    """List of hazard category codes (``["S1", "S10"]``) Llama Guard
    flagged for ``s``. Empty list when safe / detector unavailable."""
    return lg_det.llama_guard_categories(s)


def ipi_score(s: Optional[str]) -> float:
    """Indirect prompt injection score (0.0-1.0) — wraps Prompt Guard
    2 with HTML stripping, ASCII smuggling detection, and multi-
    passage scoring. The recommended ``after_tool_call`` detector
    when tool output flows back to the model.

    Always available — falls back to a regex+Unicode heuristic when
    Prompt Guard 2 isn't installed (Rule 7)."""
    return ipi_det.ipi_score(s)


def ipi_unsafe(s: Optional[str], threshold: float = 0.7) -> bool:
    """True iff IPI score >= ``threshold``. Convenience wrapper for
    ``action: rewrite`` rules at ``after_tool_call``."""
    return ipi_det.ipi_unsafe(s, threshold)


def ipi_passages(s: Optional[str]) -> list:
    """Per-passage breakdown — list of ``{"passage", "score",
    "marker"}`` dicts for the dashboard."""
    return ipi_det.ipi_passages(s)


def llm_judge(
    s: Optional[str],
    rubric: str = "Is this text safe (no malicious instructions, no data exfiltration)?",
    labels: Optional[list] = None,
) -> dict:
    """LLM-as-judge classification (Slice 3 PR R / §6.7). Calls the
    configured judge endpoint to evaluate ``s`` against ``rubric``.
    Returns ``{"label", "confidence", "rationale", "ok"}``.

    Returns the safe-by-default ``{"label": "unknown", "ok": False, ...}``
    when the endpoint isn't configured (Rule 7)."""
    return judge_det.llm_judge(s, rubric=rubric, labels=labels)


def llm_judge_unsafe(
    s: Optional[str],
    rubric: str = "Is this text safe?",
    threshold: float = 0.7,
) -> bool:
    """Convenience boolean wrapper — True iff the judge labels
    ``unsafe`` at >= threshold. Always False when the detector
    isn't available."""
    return judge_det.llm_judge_unsafe(s, rubric=rubric, threshold=threshold)


def llm_judge_label(
    s: Optional[str],
    rubric: str = "Is this text safe?",
    labels: Optional[list] = None,
) -> str:
    """Just the label string. Empty when the detector isn't
    available / failed."""
    return judge_det.llm_judge_label(s, rubric=rubric, labels=labels)


def has_image_markdown_exfil(s: Optional[str]) -> bool:
    return rp.has_image_markdown_exfil(s)


def has_ascii_smuggling(s: Optional[str]) -> bool:
    return rp.has_ascii_smuggling(s)


# Path / URL safety heuristics


_DESTRUCTIVE_PATH_TOKENS = (
    "/",          # bare root
    "/*",         # glob root
    "~",          # home shortcut alone
    "~/",         # home with trailing slash
    "/etc",
    "/usr",
    "/var",
    "/sys",
    "/proc",
    "/dev",
    "/boot",
    "/root",
    "..",         # parent traversal
    "C:\\",       # Windows root
    "C:\\Windows",
)


def is_destructive_path(path: Optional[str]) -> bool:
    """Heuristic: does ``path`` reference a system root, parent
    traversal, or other "definitely don't let the agent touch this"
    location? Used by tool-arg policies to block reckless file ops.

    Conservative — false positives here block legitimate ops, so we
    only match obvious patterns. Operators wanting tighter rules
    write their own ``regex_match(params.path, r'...')`` conditions.
    """
    if not path:
        return False
    p = str(path).strip()
    if not p:
        return False
    # Tokens we look for as the entire path or as a prefix
    for tok in _DESTRUCTIVE_PATH_TOKENS:
        if p == tok or p.startswith(tok + "/") or p.startswith(tok + "\\"):
            return True
    # Parent traversal anywhere in the path is suspicious
    if "/.." in p or "\\.." in p or p.startswith("../") or p.startswith("..\\"):
        return True
    return False


def is_internal_url(url: Optional[str]) -> bool:
    """True if ``url`` points at an internal / private host. Catches
    the common SSRF targets: localhost, RFC1918, link-local, AWS
    metadata service, file:// scheme.

    Used by web_fetch policies as the canonical "is this fetch
    going somewhere it shouldn't?" check.
    """
    if not url:
        return False
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme in ("file", "gopher", "ftp"):
        return True
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "0.0.0.0"):
        return True
    # IP-shape host? Run private_ipv4 check.
    if rp.PATTERNS["private_ipv4"].search(host):
        return True
    # IPv6 loopback / link-local
    if host in ("::1", "[::1]"):
        return True
    if host.startswith("fe80:") or host.startswith("[fe80:"):
        return True
    # AWS metadata service
    if host == "169.254.169.254" or host == "metadata.google.internal":
        return True
    return False


def redact_pii(s: Optional[str]) -> str:
    """Redact PII shapes from a string. Used inside ``rewrite``
    actions to sanitise outputs/results before they continue
    through the pipeline. Replaces matches with ``[REDACTED:kind]``
    so operators reviewing traces can see what was redacted without
    seeing the value itself."""
    if not s:
        return ""
    out = str(s)
    for kind, match in rp.regex_pii_scan(out):
        out = out.replace(match, f"[REDACTED:{kind}]")
    for kind, match in rp.secrets_in(out):
        out = out.replace(match, f"[REDACTED:{kind}]")
    return out


# ---------------------------------------------------------------------------
# History-backed builtins (factory bound to a Database connection)
# ---------------------------------------------------------------------------


# Process-local cache. Module-level for simplicity — the cache is
# keyed by (fn_name, *args) so different DBs don't collide as long as
# their session_id / trace_id / agent values are themselves unique
# (which they are by design — UUIDs).

_CACHE: Dict[tuple, tuple] = {}  # key -> (value, expires_at_ms)
_CACHE_TTL_MS = 1000


def _cached(fn_name: str, args: tuple, compute: Callable[[], Any]) -> Any:
    """1-second TTL cache for history queries. Avoids hammering DuckDB
    on bursty workloads — 100 decisions in the same second pay
    one DuckDB round-trip per unique args tuple, not 100."""
    key = (fn_name, args)
    now_ms = time.monotonic() * 1000
    cached = _CACHE.get(key)
    if cached is not None:
        value, expires_at = cached
        if now_ms < expires_at:
            return value
    try:
        value = compute()
    except Exception:
        # Rule 7: history queries that fail return safe defaults.
        # The caller's policy expression will see 0, 0.0, etc. and
        # the rule simply won't fire on this evaluation.
        logger.debug("history builtin %s failed", fn_name, exc_info=True)
        # Return type-appropriate sentinel inferred from the compute
        # function's expected output via a marker. Simpler: re-raise
        # to the bound wrapper which handles type-specific defaults.
        raise
    _CACHE[key] = (value, now_ms + _CACHE_TTL_MS)
    return value


def _safe_int(db, query: str, params: list, fn_name: str, *cache_key_args) -> int:
    def _compute() -> int:
        row = db.fetchone(query, params)
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0
    try:
        return _cached(fn_name, cache_key_args, _compute)
    except Exception:
        return 0


def _safe_float(db, query: str, params: list, fn_name: str, *cache_key_args) -> float:
    def _compute() -> float:
        row = db.fetchone(query, params)
        if row is None or row[0] is None:
            return 0.0
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return 0.0
    try:
        return _cached(fn_name, cache_key_args, _compute)
    except Exception:
        return 0.0


def build_history_builtins(db) -> Dict[str, Callable]:
    """Return the history-backed builtins map, bound to ``db``.

    The decision engine builds this once per evaluation and merges
    with the stateless map below. Tests can construct an in-memory
    DB and call this directly to exercise the queries.
    """

    def session_total_tokens(session_id: Optional[str]) -> int:
        if not session_id:
            return 0
        return _safe_int(
            db,
            "SELECT COALESCE(SUM(tokens_input + tokens_output), 0) "
            "FROM spans WHERE session_id = ?",
            [session_id],
            "session_total_tokens",
            session_id,
        )

    def session_total_cost(session_id: Optional[str]) -> float:
        if not session_id:
            return 0.0
        return _safe_float(
            db,
            "SELECT COALESCE(SUM(cost_usd), 0) "
            "FROM spans WHERE session_id = ?",
            [session_id],
            "session_total_cost",
            session_id,
        )

    def trace_total_cost(trace_id: Optional[str]) -> float:
        if not trace_id:
            return 0.0
        return _safe_float(
            db,
            "SELECT COALESCE(SUM(cost_usd), 0) "
            "FROM spans WHERE trace_id = ?",
            [trace_id],
            "trace_total_cost",
            trace_id,
        )

    def agent_calls_per_minute(agent: Optional[str]) -> int:
        """Count of spans for ``agent`` in the last 60 seconds.
        Used for rate-limit policies."""
        if not agent:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(tzinfo=None)
        # The agent identity is the trace.name — match the existing
        # convention from policy_runtime.py. spans.name is per-call,
        # not per-agent, so we join through traces.
        return _safe_int(
            db,
            "SELECT COUNT(*) FROM spans s "
            "JOIN traces t ON t.id = s.trace_id "
            "WHERE t.name = ? AND s.started_at >= ?",
            [agent, cutoff],
            "agent_calls_per_minute",
            agent,
        )

    def agent_calls_today(agent: Optional[str]) -> int:
        if not agent:
            return 0
        midnight_utc = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        return _safe_int(
            db,
            "SELECT COUNT(*) FROM spans s "
            "JOIN traces t ON t.id = s.trace_id "
            "WHERE t.name = ? AND s.started_at >= ?",
            [agent, midnight_utc],
            "agent_calls_today",
            agent,
        )

    def tool_calls_in_trace(trace_id: Optional[str], tool_name: Optional[str] = None) -> int:
        """Count of tool-type spans for a trace, optionally filtered
        by tool name. Used to detect runaway loops."""
        if not trace_id:
            return 0
        if tool_name:
            return _safe_int(
                db,
                "SELECT COUNT(*) FROM spans "
                "WHERE trace_id = ? AND type = 'tool' AND tool_name = ?",
                [trace_id, tool_name],
                "tool_calls_in_trace_named",
                trace_id, tool_name,
            )
        return _safe_int(
            db,
            "SELECT COUNT(*) FROM spans "
            "WHERE trace_id = ? AND type = 'tool'",
            [trace_id],
            "tool_calls_in_trace",
            trace_id,
        )

    def pii_violations_in_project_last_24h(project: Optional[str]) -> int:
        """Count of policy_violations rows tagged 'pii' from
        traces in this project in the last 24h. Useful for project-
        level "elevate severity if recent PII trend" policies."""
        if not project:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(tzinfo=None)
        return _safe_int(
            db,
            "SELECT COUNT(*) FROM policy_violations pv "
            "JOIN traces t ON t.id = pv.trace_id "
            "WHERE t.project = ? AND pv.created_at >= ? "
            "AND (LOWER(pv.policy_name) LIKE '%pii%' OR LOWER(pv.severity) = 'high')",
            [project, cutoff],
            "pii_violations_in_project_last_24h",
            project,
        )

    def similar_to_corpus(
        text: Optional[str],
        corpus_name: Optional[str],
        threshold: float = 0.85,
    ) -> bool:
        """Embedding similarity (Slice 3 Tier 2.4) — True iff ``text``
        is at least ``threshold`` cosine-similar to any entry in the
        operator-built corpus. Returns False when the corpus is
        missing / empty / dep not installed (Rule 7).

        Note: not cached at this layer because the corpus cache lives
        inside the embedding detector module — already process-local
        and invalidated by CRUD."""
        return emb_det.similar_to_corpus(db, text, corpus_name, threshold)

    def max_corpus_similarity(
        text: Optional[str], corpus_name: Optional[str]
    ) -> float:
        """Embedding similarity score (0.0-1.0) — useful when an
        operator wants to log the score even when below threshold,
        or stack multiple thresholds for tiered actions."""
        return emb_det.max_similarity(db, text, corpus_name)

    def behavioral_anomaly_score(
        tool_name: Optional[str],
        params: Optional[Dict[str, Any]],
        agent: Optional[str],
        session_id: Optional[str] = None,
    ) -> float:
        """Behavioral anomaly score (0..∞-ish, capped 100) for the
        current call against the rolling per-(agent, tool) baseline.
        Returns 0.0 when baseline data is sparse (Rule 7). See
        ``firewall.anomaly`` for signal composition."""
        from firewall import anomaly as fw_anomaly
        return fw_anomaly.behavioral_anomaly_score(
            db, tool_name, params, agent, session_id=session_id,
        )

    return {
        "session_total_tokens": session_total_tokens,
        "session_total_cost": session_total_cost,
        "trace_total_cost": trace_total_cost,
        "agent_calls_per_minute": agent_calls_per_minute,
        "agent_calls_today": agent_calls_today,
        "tool_calls_in_trace": tool_calls_in_trace,
        "pii_violations_in_project_last_24h": pii_violations_in_project_last_24h,
        # Embedding similarity (Slice 3 Tier 2.4 — DB-bound because
        # the corpus lives in DuckDB; see firewall.detectors.embedding)
        "similar_to_corpus": similar_to_corpus,
        "max_corpus_similarity": max_corpus_similarity,
        # Behavioral anomaly (Slice 3 PR Q — §11.4 — DB-bound
        # baseline lives in spans table)
        "behavioral_anomaly_score": behavioral_anomaly_score,
    }


# ---------------------------------------------------------------------------
# Cross-framework tool name detection (Slice 2 Tier 1.1)
# ---------------------------------------------------------------------------
#
# Different agent frameworks use different names for the same
# capability. Slice 1 dogfood caught this concretely: an OWASP rule
# matched ``tool_name == "shell"`` but OpenClaw's actual tool is
# named ``exec`` — the rule silently never fired. These builtins
# canonicalize across the common framework vocabularies so operators
# can write ``is_shell_tool()`` once and not maintain a manually-
# updated list of every tool name across every integration.
#
# When adding a new framework: extend the relevant set below. Names
# are lowercased on lookup so casing differences don't matter.

_SHELL_TOOL_NAMES = frozenset({
    # OpenClaw
    "exec",
    # Generic / many frameworks
    "shell", "bash", "sh", "terminal", "system",
    # Mastra / VoltAgent / LangChain conventions
    "run_command", "execute_command", "run_shell", "shell_exec",
    # Code-interpreter style (executes shell-like code)
    "code_exec", "code_interpreter", "python_repl", "python_exec",
})

_WEB_FETCH_TOOL_NAMES = frozenset({
    # OpenClaw / common
    "fetch", "http_fetch", "web_fetch", "http_get", "http_request",
    # LangChain / LlamaIndex
    "requests_get", "requests_post", "url_fetch",
    # cURL-named wrappers
    "curl",
    # Search-engine wrappers (often hit external URLs)
    "brave_search", "google_search", "duckduckgo_search", "web_search",
})

_DB_WRITE_TOOL_NAMES = frozenset({
    # SQL execution wrappers
    "sql_exec", "sql_query", "execute_sql", "db_execute",
    "postgres_query", "mysql_query", "sqlite_exec",
    # NoSQL
    "mongo_write", "redis_set", "redis_del",
    # ORM-style
    "db_write", "model_create", "model_update", "model_delete",
})

_FILESYSTEM_TOOL_NAMES = frozenset({
    "fs_write", "fs_read", "file_write", "file_read",
    "write_file", "read_file", "edit_file", "create_file", "delete_file",
    # OpenClaw uses "edit" for the canvas/editor tool — included
    # here because it can write to disk
    "edit",
})


def is_shell_tool(name: Optional[str]) -> bool:
    """True when the tool name is a known shell / command-execution
    tool across any supported framework. Used by firewall rules
    that want to gate on "anything that runs an arbitrary command"
    without enumerating every framework's flavor."""
    if not isinstance(name, str):
        return False
    return name.strip().lower() in _SHELL_TOOL_NAMES


def is_web_fetch_tool(name: Optional[str]) -> bool:
    """True for HTTP/URL-fetching tools across frameworks. Used in
    SSRF / data-exfil rules that want to gate on the "agent is
    reaching outside the host" capability."""
    if not isinstance(name, str):
        return False
    return name.strip().lower() in _WEB_FETCH_TOOL_NAMES


def is_db_write_tool(name: Optional[str]) -> bool:
    """True for tools that mutate a database. Used by rules guarding
    against destructive SQL (DROP, DELETE without WHERE, TRUNCATE)
    or unauthorized model-CRUD."""
    if not isinstance(name, str):
        return False
    return name.strip().lower() in _DB_WRITE_TOOL_NAMES


def is_filesystem_tool(name: Optional[str]) -> bool:
    """True for tools that read or write files outside the shell
    capability. Useful when a rule wants to catch ``write_file
    /etc/passwd`` patterns without enumerating shell commands."""
    if not isinstance(name, str):
        return False
    return name.strip().lower() in _FILESYSTEM_TOOL_NAMES


# ---------------------------------------------------------------------------
# Stateless builtins map
# ---------------------------------------------------------------------------
#
# Merged with ``build_history_builtins(db)`` to produce the full
# ``functions=`` argument for ``simpleeval.EvalWithCompoundTypes``.
# The decision engine (Slice 1 task §37) does this merge per
# evaluation.

STATELESS_BUILTINS: Dict[str, Callable] = {
    # Core string / regex
    "regex_match": regex_match,
    "regex_extract": regex_extract,
    "contains_any": contains_any,
    "len_chars": len_chars,
    "entropy": entropy,
    # URL helpers
    "url_host": url_host,
    "url_in_allowlist": url_in_allowlist,
    "is_internal_url": is_internal_url,
    # Path safety
    "is_destructive_path": is_destructive_path,
    # Detectors (regex-pack-backed)
    "looks_like_secret": looks_like_secret,
    "has_pii": has_pii,
    "has_image_markdown_exfil": has_image_markdown_exfil,
    "has_ascii_smuggling": has_ascii_smuggling,
    # Detectors (presidio — optional, returns 0.0 / [] when not installed)
    "presidio_pii_score": presidio_pii_score,
    "presidio_pii_entities": presidio_pii_entities,
    # Detectors (Prompt Guard 2 — optional, returns 0.0 / "" when not installed)
    "prompt_guard_score": prompt_guard_score,
    "prompt_guard_label": prompt_guard_label,
    # Detectors (Llama Guard 4 — optional, safe-by-default when not installed)
    "llama_guard_classify": llama_guard_classify,
    "llama_guard_unsafe": llama_guard_unsafe,
    "llama_guard_categories": llama_guard_categories,
    # Detectors (IPI sniffer — always-on, escalates with Prompt Guard 2)
    "ipi_score": ipi_score,
    "ipi_unsafe": ipi_unsafe,
    "ipi_passages": ipi_passages,
    # Detectors (LLM-as-judge — Slice 3 PR R, §6.7; optional, requires
    # LUMIN_LLM_JUDGE_ENDPOINT to be set)
    "llm_judge": llm_judge,
    "llm_judge_unsafe": llm_judge_unsafe,
    "llm_judge_label": llm_judge_label,
    # Sanitisation
    "redact_pii": redact_pii,
    # Cross-framework tool name canonicalization (Slice 2 Tier 1.1).
    # Operators write ``is_shell_tool()`` once instead of
    # ``tool_name in ["exec","shell","bash","sh","terminal",...]``.
    "is_shell_tool": is_shell_tool,
    "is_web_fetch_tool": is_web_fetch_tool,
    "is_db_write_tool": is_db_write_tool,
    "is_filesystem_tool": is_filesystem_tool,
}


def reset_cache_for_tests() -> None:
    """Clear the history-builtin cache. Called between tests so a
    fixture reusing process-level state doesn't see stale answers
    from a previous test's DB."""
    _CACHE.clear()
