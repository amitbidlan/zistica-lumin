"""Policy Engine — Accountability Layer Part B.

Lumin records what happened. The Policy Engine adds the question
"should this have happened?": developers define rules in a YAML file,
and the engine evaluates each rule after every span and trace ends.
Violations are surfaced as red badges in the dashboard, queryable via
``GET /v1/violations``, and optionally pushed to a webhook URL.

This module is import-safe even when ``simpleeval`` / ``pyyaml`` are
missing — the load helpers raise a clean error and the engine stays
disabled, per Rule 7 (agent must never fail because of Lumin).

Security note (Rule 7-adjacent):
    Conditions are arbitrary user-supplied expressions. We MUST NOT
    use ``eval()`` — that would let a policy file pwn the agent
    process. ``simpleeval`` parses the expression into an AST and
    only evaluates a closed set of operators + a small whitelist of
    functions (len, str, int, float, abs). Names like ``__import__``,
    ``open``, ``exec`` are not in scope and dotted attribute access
    is rejected by default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("lumin.policy")


# --- public types ------------------------------------------------------------


VALID_TRIGGERS = {"span_end", "trace_end"}
# Action vocabulary spans both layers:
#   - post_ingest (legacy): flag, alert — advisory, recorded as
#     PolicyViolation rows after the span lands.
#   - Agent Firewall (per AGENT_FIREWALL_SPEC.md §3.3): allow, block,
#     flag, require_approval, rewrite — synchronous decisions returned
#     by /v1/policy/decide for proxy/tool lifecycles.
# Both vocabularies coexist on the same Policy.action field. The
# decide engine and the post-ingest engine each interpret only the
# subset relevant to their lifecycle; cross-vocab misuse is caught
# at validation time in the firewall path.
VALID_ACTIONS = {
    "flag", "alert",
    "allow", "block", "require_approval", "rewrite",
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}


@dataclass
class Policy:
    """One rule from the policy YAML.

    Fields match the YAML schema documented in the session prompt.
    ``webhook_url`` is optional; when missing, alerts fall back to the
    global ``alert_webhook`` configured via ``lumin.configure(...)``.

    ``scope_agents`` (Phase 3) limits the policy to a list of agent names
    (matched against ``trace.name`` — the agent identity in Lumin's
    Phase-1 model). Empty list = un-scoped = applies to every agent.

    Agent Firewall fields (added in Slice 1 of the firewall pivot,
    see ``docs/AGENT_FIREWALL_SPEC.md``) — all optional with
    back-compat defaults so older SDK users authoring rules without
    them keep the legacy post-ingest advisory behavior unchanged:

    - ``lifecycle`` selects WHEN the rule fires:
      ``post_ingest`` (legacy, default), ``before_proxy_call``,
      ``after_proxy_call``, ``before_tool_call``, ``after_tool_call``.
    - ``mode`` selects WHETHER the rule's action takes effect:
      ``shadow`` (record only, never block), ``flag`` (record as
      violation but don't block), ``enforce`` (do the real action).
    - ``priority`` orders rules within a lifecycle: higher fires
      first; an explicit ``allow`` short-circuits lower-priority
      rules.
    - ``on_timeout`` is the fallback when an approval round-trip
      exceeds its timeout: ``allow`` or ``deny``.
    - ``on_internal_error`` is the fallback when the engine itself
      errors during evaluation: ``allow`` (Rule 7 default) or
      ``deny`` for high-severity rules.
    - ``circuit_breaker_state`` tracks runaway rules — when set to
      ``tripped``, the rule is silently demoted to shadow until an
      operator resets it via the dashboard.
    - ``stream_behavior`` (after_proxy_call only) — controls how
      streaming responses interact with policy enforcement: ``flag``
      (default; record but never block; lossless UX), ``cancel``
      (close the SSE mid-stream — partial leak still possible),
      ``buffer`` (disable streaming for this lifecycle entirely;
      full enforcement, slower UX).
    """

    name: str
    trigger: str
    condition: str
    action: str
    severity: str
    description: Optional[str] = None
    webhook_url: Optional[str] = None
    scope_agents: List[str] = field(default_factory=list)

    # ----- Agent Firewall fields ---------------------------------------
    lifecycle: str = "post_ingest"
    mode: str = "enforce"
    priority: int = 0
    on_timeout: str = "allow"
    on_internal_error: str = "allow"
    circuit_breaker_state: str = "ok"
    stream_behavior: str = "flag"

    def applies_to_agent(self, agent_name: Optional[str]) -> bool:
        """Whether this policy should fire for an event from `agent_name`.

        - Empty ``scope_agents`` → applies to every agent (default).
        - Non-empty + ``agent_name`` is in the list → applies.
        - Non-empty + ``agent_name`` is None or unmatched → skipped.

        ``agent_name=None`` deliberately *blocks* a scoped rule: we don't
        know which agent triggered the event yet, so we err on the side
        of not firing — better a missed scoped check than a wrong-agent
        violation showing up against the unknown trace.
        """
        if not self.scope_agents:
            return True
        if not agent_name:
            return False
        return agent_name in self.scope_agents


@dataclass
class PolicyViolation:
    """A single policy that fired against a span or trace.

    The SDK builds this and POSTs it to the API at /v1/violations. The
    API uses the same shape (plus an ``id`` and ``created_at`` it fills
    in) when reading from the policy_violations table.
    """

    policy_name: str
    severity: str
    trace_id: str
    condition_text: str
    action_taken: str
    span_id: Optional[str] = None
    policy_description: Optional[str] = None
    webhook_url: Optional[str] = None
    actual_value: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "policy_name": self.policy_name,
            "policy_description": self.policy_description,
            "severity": self.severity,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "condition_text": self.condition_text,
            "action_taken": self.action_taken,
            "webhook_url": self.webhook_url,
            "actual_value": self.actual_value,
            "created_at": self.created_at,
        }


# --- exceptions --------------------------------------------------------------


class PolicyConfigError(ValueError):
    """Raised when the policy file is malformed.

    Surfaced at configure() time so the developer sees the problem
    immediately. At runtime the engine swallows everything — we never
    let a broken policy file crash the agent.
    """


# --- internal helpers --------------------------------------------------------


def _validate_policy_dict(d: dict, idx: int) -> Policy:
    """Convert one parsed YAML entry into a Policy or raise PolicyConfigError."""
    if not isinstance(d, dict):
        raise PolicyConfigError(
            f"policies[{idx}]: each policy must be a mapping, got {type(d).__name__}"
        )

    required = ("name", "trigger", "condition", "action", "severity")
    missing = [k for k in required if k not in d or d[k] in (None, "")]
    if missing:
        raise PolicyConfigError(
            f"policies[{idx}]: missing required field(s): {', '.join(missing)}"
        )

    trigger = d["trigger"]
    if trigger not in VALID_TRIGGERS:
        raise PolicyConfigError(
            f"policies[{idx}] '{d['name']}': trigger must be one of "
            f"{sorted(VALID_TRIGGERS)}, got {trigger!r}"
        )

    action = d["action"]
    if action not in VALID_ACTIONS:
        raise PolicyConfigError(
            f"policies[{idx}] '{d['name']}': action must be one of "
            f"{sorted(VALID_ACTIONS)}, got {action!r}"
        )

    severity = d["severity"]
    if severity not in VALID_SEVERITIES:
        raise PolicyConfigError(
            f"policies[{idx}] '{d['name']}': severity must be one of "
            f"{sorted(VALID_SEVERITIES)}, got {severity!r}"
        )

    condition = d["condition"]
    if not isinstance(condition, str) or not condition.strip():
        raise PolicyConfigError(
            f"policies[{idx}] '{d['name']}': condition must be a non-empty string"
        )

    scope_agents = _parse_scope_agents(d, idx)

    # Agent Firewall fields (per AGENT_FIREWALL_SPEC.md §3) — all
    # optional with safe defaults. Validation is intentionally permissive
    # here: bad values fall back to defaults rather than rejecting the
    # whole policy file, since legacy YAML pre-dates these fields.
    lifecycle = d.get("lifecycle") or "post_ingest"
    mode = d.get("mode") or "enforce"
    try:
        priority = int(d.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    on_timeout = d.get("on_timeout") or "allow"
    on_internal_error = d.get("on_internal_error") or "allow"

    return Policy(
        name=str(d["name"]),
        description=d.get("description"),
        trigger=trigger,
        condition=condition,
        action=action,
        severity=severity,
        webhook_url=d.get("webhook_url"),
        scope_agents=scope_agents,
        lifecycle=lifecycle,
        mode=mode,
        priority=priority,
        on_timeout=on_timeout,
        on_internal_error=on_internal_error,
    )


def _parse_scope_agents(d: dict, idx: int) -> List[str]:
    """Extract ``scope.agents`` from a parsed policy YAML entry.

    Schema:
        scope:
          agents: [list of strings]

    All fields are optional. Missing scope or empty list = un-scoped =
    applies to every agent. Validates strictly so a typo in the YAML
    is caught at load time, not at first-fire.
    """
    scope = d.get("scope")
    if scope is None:
        return []
    if not isinstance(scope, dict):
        raise PolicyConfigError(
            f"policies[{idx}] '{d.get('name')}': scope must be a mapping, "
            f"got {type(scope).__name__}"
        )
    raw = scope.get("agents")
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise PolicyConfigError(
            f"policies[{idx}] '{d.get('name')}': scope.agents must be a list, "
            f"got {type(raw).__name__}"
        )
    out: List[str] = []
    for i, a in enumerate(raw):
        if not isinstance(a, str) or not a.strip():
            raise PolicyConfigError(
                f"policies[{idx}] '{d.get('name')}': "
                f"scope.agents[{i}] must be a non-empty string"
            )
        out.append(a.strip())
    return out


class _AttrNamespace:
    """Tiny object that exposes a dict's keys as attributes.

    Used to give simpleeval expressions like ``span.duration_ms`` a
    target to read from. Built from a plain dict so we keep the
    surface area tightly controlled — the engine decides which keys
    a condition can see; the caller's full Span object never reaches
    the evaluator.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        # Return None for any field we deliberately didn't populate
        # (e.g. tokens_input on a non-LLM span). simpleeval handles
        # ``None > 100`` cleanly by raising TypeError, which the
        # engine catches and treats as "policy did not fire".
        return self._data.get(name)


def _span_namespace(span: Any) -> _AttrNamespace:
    """Project an SDK Span (or any Span-like object) onto the field
    surface the policy condition is allowed to see.

    Computes ``duration_ms`` from started_at/ended_at if not already
    set. Reads optional rich fields (tokens_*, cost_usd, model) when
    present on _ExtSpan; defaults to None otherwise.
    """
    # Accept either an object or a plain dict so callers can build a
    # namespace from the API's wire format too.
    if isinstance(span, dict):
        get = span.get
    else:
        def get(k, default=None):
            return getattr(span, k, default)

    started = get("started_at")
    ended = get("ended_at")
    duration_ms = _compute_duration_ms(started, ended)

    return _AttrNamespace({
        "id": get("id"),
        "trace_id": get("trace_id"),
        "parent_span_id": get("parent_span_id"),
        "name": get("name"),
        "type": get("type"),
        "input": get("input"),
        "output": get("output"),
        "duration_ms": duration_ms,
        "status": get("status") or ("error" if get("error") else "ok"),
        "model": get("model"),
        "provider": get("provider"),
        "tokens_input": get("tokens_input"),
        "tokens_output": get("tokens_output"),
        "cost_usd": get("cost_usd"),
        "tool_name": get("tool_name"),
        "session_id": get("session_id"),
        "span_subtype": get("span_subtype"),
        "thinking_tokens": get("thinking_tokens"),
    })


def _trace_namespace(trace: Any) -> _AttrNamespace:
    if isinstance(trace, dict):
        get = trace.get
    else:
        def get(k, default=None):
            return getattr(trace, k, default)

    started = get("started_at")
    ended = get("ended_at")
    duration_ms = get("duration_ms")
    if duration_ms is None:
        duration_ms = _compute_duration_ms(started, ended)

    return _AttrNamespace({
        "id": get("id") or get("trace_id"),
        "name": get("name"),
        "input": get("input"),
        "output": get("output"),
        "total_cost_usd": (get("total_cost_usd") or 0.0),
        "total_tokens": (get("total_tokens") or 0),
        "duration_ms": duration_ms,
        "span_count": (get("span_count") or 0),
        "error_count": (get("error_count") or 0),
        "session_id": get("session_id"),
        "user_id": get("user_id"),
    })


def _compute_duration_ms(started: Any, ended: Any) -> Optional[int]:
    """Compute duration in milliseconds from started_at + ended_at.

    Accepts ISO strings, ``datetime`` objects, or numeric epoch ms.
    Returns None if either timestamp is missing or unparsable.
    """
    if started is None or ended is None:
        return None
    try:
        s = _parse_ts(started)
        e = _parse_ts(ended)
        if s is None or e is None:
            return None
        delta = e - s
        return int(delta.total_seconds() * 1000)
    except Exception:
        return None


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Treat numeric values as epoch seconds (most common) — if it
        # looks like ms, scale down. Anything more than 10^12 is
        # almost certainly milliseconds; year > 33658 in seconds.
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(value, str):
        s = value
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


# --- engine ------------------------------------------------------------------


# Whitelisted functions exposed to policy condition expressions. Anything
# not in this dict is rejected by simpleeval before evaluation.
_SAFE_FUNCTIONS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "abs": abs,
}


class PolicyEngine:
    """Evaluates policies against spans and traces.

    The engine owns no I/O — it doesn't fetch from the DB, doesn't
    POST violations, doesn't fire webhooks. It just produces
    PolicyViolation objects. The SDK is responsible for shipping
    them.
    """

    def __init__(self, policy_file: Union[str, Path]):
        """Load policies from a YAML file.

        Raises ``PolicyConfigError`` if the file is missing,
        unreadable, or contains invalid policy entries. The SDK's
        configure() catches this error and disables the engine
        cleanly — the agent still runs.
        """
        path = Path(policy_file)
        if not path.exists():
            raise PolicyConfigError(f"policy file not found: {path}")

        try:
            import yaml
        except ImportError as e:
            raise PolicyConfigError(
                "pyyaml is required for policy file parsing — "
                "reinstall lumin to pull in the dep"
            ) from e

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise PolicyConfigError(f"policy file is not valid YAML: {e}") from e
        except OSError as e:
            raise PolicyConfigError(f"could not read policy file {path}: {e}") from e

        self._path = path
        self._policies = self._parse_policies(raw)
        # Pre-parse each condition's AST once at load time. simpleeval
        # accepts ``eval(expr, previously_parsed=ast)`` to skip the
        # ast.parse call per evaluation. With the AST cache, hot-path
        # evaluations drop from ~56μs to ~30μs each (measured) — at
        # 18k spans/sec that's 50% headroom for free, and the per-call
        # work becomes proportional to operator count not condition
        # length. Catches syntax errors at load time too.
        # EvalWithCompoundTypes supports list/dict/set literals + the
        # `in` operator. Lets operators author conditions like
        # ``span.model in ["gpt-4o", "claude-sonnet-4"]`` instead of
        # OR-chains. Same security model — function whitelist still
        # gates what's callable.
        from simpleeval import EvalWithCompoundTypes
        _parser = EvalWithCompoundTypes(functions=dict(_SAFE_FUNCTIONS))
        self._compiled: List[tuple] = []
        for p in self._policies:
            try:
                ast_node = _parser.parse(p.condition)
            except Exception as e:
                # Bad condition syntax — log and skip this policy. The
                # engine stays usable, just with one fewer rule. We
                # keep going so a single typo doesn't disable
                # enforcement entirely.
                logger.warning(
                    "policy %r: condition unparseable, skipping: %s",
                    p.name, e,
                )
                continue
            self._compiled.append((p, ast_node))

    @property
    def policies(self) -> List[Policy]:
        return list(self._policies)

    @staticmethod
    def _parse_policies(raw: Any) -> List[Policy]:
        if raw is None:
            # Empty file = zero policies = engine is a no-op. Not an error.
            return []
        if not isinstance(raw, dict):
            raise PolicyConfigError(
                "policy file must be a mapping at the top level "
                "(got {type})".format(type=type(raw).__name__)
            )
        version = raw.get("version")
        if version not in (1, "1", None):
            raise PolicyConfigError(
                f"unsupported policy file version {version!r} — only version 1 is supported"
            )
        policies_raw = raw.get("policies", [])
        if policies_raw in (None, []):
            return []
        if not isinstance(policies_raw, list):
            raise PolicyConfigError("'policies' must be a list")

        return [_validate_policy_dict(p, i) for i, p in enumerate(policies_raw)]

    @staticmethod
    def _make_evaluator():
        """Build a fresh simpleeval evaluator per call.

        EvalWithCompoundTypes (vs plain SimpleEval) is stateful
        (caches names) so we don't reuse one evaluator across
        spans — but we DO reuse one per evaluation within a single
        call. The compound-types variant unlocks list/set/dict
        literals + `in` operator, used widely in firewall rules.
        """
        from simpleeval import EvalWithCompoundTypes

        ev = EvalWithCompoundTypes(functions=dict(_SAFE_FUNCTIONS))
        return ev

    def evaluate_span(
        self, span: Any, agent_name: Optional[str] = None
    ) -> List[PolicyViolation]:
        """Run all span_end policies against `span`.

        `span` may be the SDK's Span dataclass, an _ExtSpan, or a
        plain dict (the API's wire format). Returns a list of
        violations — empty if none fired.

        ``agent_name`` (Phase 3) is the trace's name — used to filter
        out scoped policies whose ``scope.agents`` doesn't include
        this agent. Pass ``None`` to evaluate only un-scoped rules.

        Errors in any single condition are logged + skipped; one bad
        policy never blocks the others.
        """
        return self._evaluate(
            span, trigger="span_end", build_ns=_span_namespace, agent_name=agent_name,
        )

    def evaluate_trace(
        self, trace: Any, agent_name: Optional[str] = None
    ) -> List[PolicyViolation]:
        """Run all trace_end policies against `trace`. See ``evaluate_span``
        for the ``agent_name`` semantics."""
        return self._evaluate(
            trace, trigger="trace_end", build_ns=_trace_namespace, agent_name=agent_name,
        )

    def evaluate_spans_batch(
        self, spans: List[Any], agent_name: Optional[str] = None
    ) -> List[PolicyViolation]:
        """Batch-evaluate span_end policies against many spans.

        Equivalent to ``[v for s in spans for v in self.evaluate_span(s)]``
        but cheaper at the margin: amortizes engine setup, returns a
        flat list. The AST cache (initialized at load time) is the
        actual perf win — this method exists so callers can pass a
        whole batch to a single function rather than looping outside.

        All spans in the batch are assumed to belong to the same agent
        (the SDK only ever calls this from one trace at a time). Server-
        side callers that mix agents should call ``evaluate_span`` per
        span with the right ``agent_name``.
        """
        if not self._compiled:
            return []
        out: List[PolicyViolation] = []
        for s in spans:
            out.extend(self._evaluate(s, "span_end", _span_namespace, agent_name=agent_name))
        return out

    def _evaluate(
        self,
        target: Any,
        trigger: str,
        build_ns,
        agent_name: Optional[str] = None,
    ) -> List[PolicyViolation]:
        from simpleeval import (
            FunctionNotDefined,
            InvalidExpression,
            NameNotDefined,
        )

        if not self._policies:
            return []

        violations: List[PolicyViolation] = []
        ns = build_ns(target)

        # Resolve identifiers used in the namespace ("span" or "trace")
        # so simpleeval can read them. We hand simpleeval the entire
        # namespace under one name — simpleeval supports attribute
        # access for whitelisted top-level names by default.
        names: Dict[str, Any] = {trigger.split("_")[0]: ns}  # "span" or "trace"

        # Resolve trace_id and span_id once for violation records
        trace_id = self._resolve_trace_id(target)
        span_id = self._resolve_span_id(target) if trigger == "span_end" else None

        # Iterate (policy, ast) tuples — AST is pre-parsed at load time
        # and reused on every evaluation. SimpleEval is constructed
        # fresh per call (cheap class instantiation, no shared mutable
        # state across threads).
        for policy, ast_node in self._compiled:
            if policy.trigger != trigger:
                continue
            # Phase-3 scope: skip rules whose scope.agents excludes this
            # agent. Un-scoped rules (default) always pass this gate.
            if not policy.applies_to_agent(agent_name):
                continue
            try:
                ev = self._make_evaluator()
                ev.names = names
                result = ev.eval(policy.condition, previously_parsed=ast_node)
            except (NameNotDefined, FunctionNotDefined) as e:
                # Reference to an unknown name — the policy author
                # asked for something we don't expose. Log + skip.
                logger.warning(
                    "policy %r: condition references unknown name (%s); skipping",
                    policy.name, e,
                )
                continue
            except InvalidExpression as e:
                logger.warning(
                    "policy %r: invalid expression: %s; skipping",
                    policy.name, e,
                )
                continue
            except TypeError:
                # ``None > 100`` and similar — happens when the policy
                # condition references a field that's None on this
                # span (e.g. tokens_input on a custom span). Treat as
                # "policy did not fire" — same as a clean False.
                continue
            except Exception:
                logger.exception(
                    "policy %r: unexpected error evaluating condition; skipping",
                    policy.name,
                )
                continue

            if result:
                violations.append(
                    PolicyViolation(
                        policy_name=policy.name,
                        policy_description=policy.description,
                        severity=policy.severity,
                        trace_id=trace_id or "",
                        span_id=span_id,
                        condition_text=policy.condition,
                        action_taken=policy.action,
                        webhook_url=policy.webhook_url,
                        actual_value=self._snapshot_actual(policy.condition, ns),
                    )
                )

        return violations

    @staticmethod
    def _resolve_trace_id(target: Any) -> Optional[str]:
        if isinstance(target, dict):
            return target.get("trace_id") or target.get("id")
        return getattr(target, "trace_id", None) or getattr(target, "id", None)

    @staticmethod
    def _resolve_span_id(target: Any) -> Optional[str]:
        if isinstance(target, dict):
            return target.get("id")
        return getattr(target, "id", None)

    @staticmethod
    def _snapshot_actual(condition: str, ns: _AttrNamespace) -> Optional[str]:
        """Best-effort: capture the LHS field's actual value so the
        violation record + webhook payload include real numbers.

        Heuristic only — we look for ``span.X`` or ``trace.X`` as the
        first identifier in the condition and fetch X. If the
        condition doesn't fit that pattern, we return None and the
        webhook payload omits actual_value.
        """
        parts = condition.strip().split(maxsplit=1)
        if not parts:
            return None
        head = parts[0]
        if "." not in head:
            return None
        prefix, _, rest = head.partition(".")
        if prefix not in ("span", "trace"):
            return None
        # Take just the bare identifier — strip parens, brackets, etc.
        field_name = ""
        for ch in rest:
            if ch.isalnum() or ch == "_":
                field_name += ch
            else:
                break
        if not field_name:
            return None
        value = getattr(ns, field_name)
        if value is None:
            return None
        return str(value)


def load_policy_engine(policy_file: Union[str, Path, None]) -> Optional[PolicyEngine]:
    """Helper used by SDK configure() — returns ``None`` cleanly when
    no policy file is configured. Errors during load propagate to the
    caller so the developer sees a clear startup error; the SDK then
    decides whether to disable the engine or re-raise.
    """
    if not policy_file:
        return None
    return PolicyEngine(policy_file)
