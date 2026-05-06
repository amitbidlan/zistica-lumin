"""OTLP decode + translate — converts OpenTelemetry trace exports
into Lumin's native ``SpanInput`` shape.

Pure functions only. No I/O, no DB, no FastAPI imports here. The
HTTP router (``routers/otlp.py``) calls into this module and then
hands the resulting ``SpanInput`` list to the same insert path
``POST /v1/spans`` already uses — so DuckDB writes, WS broadcasts,
policy evaluation, and dashboard rendering all flow unchanged.

Why a separate module?

  - Tests can exercise the decode logic without spinning up FastAPI.
    Most of the wire-format risk lives here (hex IDs, nano-second
    timestamps, protobuf field types, ``gen_ai.*`` attribute
    fan-out). Keeping it pure keeps the test surface small.

  - Future ingest paths (e.g. OTLP/gRPC, when v2 needs it) can
    reuse the same translation by calling ``parse_otlp_proto`` /
    ``parse_otlp_json`` directly. The HTTP router becomes a thin
    transport wrapper.

The two entry points are symmetric:

  parse_otlp_proto(bytes)  -> List[SpanInput], project_hint
  parse_otlp_json(dict)    -> List[SpanInput], project_hint

Each returns a tuple — the second element is the resource's
``service.name`` (or None), which the router uses as a fallback for
``X-Lumin-Project`` resolution.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from models import SpanInput

logger = logging.getLogger("lumin.api.otlp")


# ---- pricing tables -------------------------------------------------------
#
# Same families as the integration packages keep, deliberately small so
# unknown models return ``cost_usd = None`` instead of fabricating a
# number. Pricing is in USD per 1,000,000 tokens (matches the OpenAI /
# Anthropic published pricing pages — easier to update without unit
# conversions). The compute step divides by 1e6 at the end.


_OPENAI_PRICING: Dict[str, Tuple[float, float]] = {
    "gpt-4o":           (2.50, 10.00),
    "gpt-4o-mini":      (0.15,  0.60),
    "gpt-4-turbo":     (10.00, 30.00),
    "gpt-4":           (30.00, 60.00),
    "gpt-3.5-turbo":    (0.50,  1.50),
}

_ANTHROPIC_PRICING: Dict[str, Tuple[float, float]] = {
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-4":    (3.00, 15.00),
    "claude-haiku-4":     (0.80,  4.00),
}


def _compute_cost(
    model: Optional[str],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
) -> Optional[float]:
    """Cost in USD, longest-prefix match against the pricing tables.

    Returns None when:
      - model is missing, or
      - either token count is missing, or
      - no prefix in the tables matches the model name.

    Never guesses — a None cost in the dashboard is more honest than
    a fabricated number from the wrong family.
    """
    if not model or tokens_in is None or tokens_out is None:
        return None
    m = model.lower()
    best_key = ""
    best: Optional[Tuple[float, float]] = None
    for table in (_OPENAI_PRICING, _ANTHROPIC_PRICING):
        for key, prices in table.items():
            if m.startswith(key) and len(key) > len(best_key):
                best_key = key
                best = prices
    if best is None:
        return None
    inp, outp = best
    return round((tokens_in * inp + tokens_out * outp) / 1_000_000, 8)


# ---- ID + timestamp normalization -----------------------------------------


def _hex_to_uuid(hex_id: str) -> str:
    """Convert an OTel hex span/trace ID into a UUID-shaped string.

    OTel uses 32-char hex for trace_id and 16-char hex for span_id.
    Lumin's existing schema stores ids as VARCHAR — we don't strictly
    need UUID format, but rendering as 8-4-4-4-12 reads well in the
    dashboard and matches what the SDK already emits.

    Short hex (16-char span_id) is left-padded with zeros so the
    output is still UUID-shaped — keeps the column visually
    consistent even though the bottom half is just padding.
    """
    h = (hex_id or "").lower().strip()
    if not h:
        return ""
    if len(h) < 32:
        h = h.rjust(32, "0")
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _nanos_to_iso(nano: Any) -> Optional[str]:
    """Convert nanosecond UNIX time (string or int, per OTel spec) to
    a naive UTC ISO string — matches what the rest of the API stores."""
    if nano is None:
        return None
    try:
        n = int(nano)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    seconds = n / 1_000_000_000
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    # Match the precision the rest of the API expects (microseconds + Z)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---- attribute access helpers ---------------------------------------------
#
# OTLP/JSON uses a verbose ``[{"key": k, "value": {"stringValue": v}}]``
# array; OTLP/proto uses ``[KeyValue(key=k, value=AnyValue(...))]``. The
# protobuf objects expose ``.WhichOneof('value')`` and a typed accessor.
# Normalize both into a plain ``dict[str, Any]`` so the rest of the
# decode pipeline doesn't branch on transport.


def _attrs_from_proto(proto_attrs: Any) -> Dict[str, Any]:
    """Materialize a ``KeyValue[]`` from protobuf into a Python dict.

    The protobuf ``AnyValue`` is a oneof with seven cases — string,
    bool, int, double, array, kvlist, bytes. We unpack each so the
    caller doesn't have to import protobuf classes.
    """
    out: Dict[str, Any] = {}
    for kv in proto_attrs:
        try:
            out[kv.key] = _anyvalue_to_python(kv.value)
        except Exception:
            # Malformed attribute — log and skip rather than abort the
            # whole batch. OTel implementations have shipped invalid
            # protobuf in the wild before.
            logger.debug("otlp: skipping unparseable attribute %r", kv.key)
    return out


def _anyvalue_to_python(value: Any) -> Any:
    which = value.WhichOneof("value")
    if which == "string_value":
        return value.string_value
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return int(value.int_value)
    if which == "double_value":
        return float(value.double_value)
    if which == "bytes_value":
        return bytes(value.bytes_value)
    if which == "array_value":
        return [_anyvalue_to_python(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _anyvalue_to_python(kv.value) for kv in value.kvlist_value.values}
    return None


def _attrs_from_json(json_attrs: List[dict]) -> Dict[str, Any]:
    """Same shape but for the OTLP/JSON wire format."""
    out: Dict[str, Any] = {}
    for kv in json_attrs or []:
        try:
            out[kv["key"]] = _jsonvalue_to_python(kv.get("value") or {})
        except Exception:
            logger.debug("otlp: skipping unparseable JSON attribute")
    return out


def _jsonvalue_to_python(value: dict) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        # OTLP/JSON encodes int64 as a string (per the proto3 → JSON
        # mapping rule) — coerce defensively.
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "bytesValue" in value:
        # base64 string per spec; expose raw for the rare attribute that needs it
        return value["bytesValue"]
    if "arrayValue" in value:
        return [_jsonvalue_to_python(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return _attrs_from_json(value["kvlistValue"].get("values", []))
    return None


# ---- the actual translation -----------------------------------------------


# OTel SpanKind enum values. We only special-case CLIENT (LLM/HTTP egress
# from the agent's POV) and SERVER (incoming work, treat as custom).
_SPAN_KIND_CLIENT = 3
_SPAN_KIND_SERVER = 2


def _classify_type(kind: int, attrs: Dict[str, Any], name: Optional[str] = None) -> str:
    """Best-effort span-type classification.

    Priority (first match wins):
      tool.name / gen_ai.tool.name / openclaw.tool.name → tool
      db.system                                          → retrieval
      span name 'openclaw.tool.*'                        → tool
      span name 'openclaw.model.*'                       → llm
      gen_ai.* attributes present + ANY span kind        → llm
      span name 'openclaw.exec'                          → custom
      otherwise                                          → custom

    Why span-name patterns: OpenClaw's diagnostics-otel emits model
    calls with SpanKind.INTERNAL (kind=1) rather than CLIENT (kind=3).
    The strict-OTel rule "CLIENT + gen_ai.* → llm" misses every one
    of them — operators see "type=custom" on what's clearly an LLM
    call. The span name itself is a reliable signal.

    Why we relaxed the kind requirement on gen_ai.*: tools beyond
    OpenClaw also emit gen_ai attributes on INTERNAL spans (Logfire
    does this for in-process Pydantic-AI calls, for example). Strict
    kind matching kept producing too many type=custom rows.
    """
    if "tool.name" in attrs or "gen_ai.tool.name" in attrs or "openclaw.tool.name" in attrs:
        return "tool"
    if "db.system" in attrs:
        return "retrieval"
    if name:
        if name.startswith("openclaw.tool."):
            return "tool"
        if name.startswith("openclaw.model.") or name.startswith("gen_ai."):
            return "llm"
    has_gen_ai = any(k.startswith("gen_ai.") for k in attrs)
    if has_gen_ai:
        return "llm"
    return "custom"


def _stringify(value: Any, max_len: int = 32_768) -> Optional[str]:
    """Coerce attribute values into a string suitable for span.input /
    span.output. JSON-encode lists / dicts so they round-trip; cap
    length so a runaway prompt doesn't bloat DuckDB rows.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except Exception:
            s = str(value)
    return s if len(s) <= max_len else s[:max_len]


def _extract_input(attrs: Dict[str, Any]) -> Optional[str]:
    """Look for input/prompt content under any of the conventions we
    know about. Order matters — first hit wins.

    Includes OpenClaw's `openclaw.content.*` and `openclaw.input` keys
    so spans from `@openclaw/diagnostics-otel` carry their actual
    prompt text into the dashboard. Without these, the OpenClaw
    integration showed empty input columns even though the LLM call
    plainly had a prompt — see PR follow-up to PR #30.
    """
    for k in (
        "gen_ai.prompt",
        "gen_ai.input.messages",
        "input.value",
        "llm.input_messages",
        "ai.prompt.messages",
        "openclaw.content.input_messages",
        "openclaw.content.system_prompt",
        "openclaw.content.tool_input",
        "openclaw.input",
    ):
        if k in attrs:
            return _stringify(attrs[k])
    return None


def _extract_output(attrs: Dict[str, Any]) -> Optional[str]:
    for k in (
        "gen_ai.completion",
        "gen_ai.output.messages",
        "output.value",
        "llm.output_messages",
        "ai.response.text",
        "openclaw.content.output_messages",
        "openclaw.content.tool_output",
        "openclaw.output",
    ):
        if k in attrs:
            return _stringify(attrs[k])
    return None


def _extract_session_id(attrs: Dict[str, Any]) -> Optional[str]:
    """Resolve session_id from OpenClaw conventions.

    OpenClaw groups multi-turn conversations under `openclaw.session_id`
    (per-thread) or `openclaw.channel.id` (per-channel). Whichever is
    present wins. Lumin's session view groups traces with shared
    session_id, so populating this lets the /sessions page work for
    OpenClaw chat threads without any user config.
    """
    for k in ("openclaw.session_id", "openclaw.channel.id"):
        v = attrs.get(k)
        if isinstance(v, str) and v:
            return v
    return None


_KNOWN_KEYS = frozenset({
    "gen_ai.system",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.operation.name",
    "gen_ai.tool.name",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "input.value",
    "output.value",
    "llm.input_messages",
    "llm.output_messages",
    "ai.prompt.messages",
    # OpenClaw native attributes — the diagnostics-otel plugin emits
    # these on every model.call / tool.execution span. Stripping them
    # from metadata so first-class fields don't double up.
    "openclaw.provider",
    "openclaw.model",
    "openclaw.tool.name",
    "openclaw.content.input_messages",
    "openclaw.content.system_prompt",
    "openclaw.content.tool_input",
    "openclaw.content.output_messages",
    "openclaw.content.tool_output",
    "openclaw.input",
    "openclaw.output",
    "openclaw.session_id",
    "openclaw.channel.id",
    "ai.response.text",
    "tool.name",
    "db.system",
})


def _build_metadata(attrs: Dict[str, Any]) -> Optional[dict]:
    """Anything we didn't lift into a first-class field gets stored in
    metadata so it's still queryable. Skip the keys we already
    extracted to keep payload size sane.
    """
    extra = {k: v for k, v in attrs.items() if k not in _KNOWN_KEYS}
    return extra or None


# ---- Status code → Lumin status -------------------------------------------
#
# OTel StatusCode: 0 = UNSET, 1 = OK, 2 = ERROR


def _status_from_otel(code: Any, message: Optional[str]) -> Tuple[str, Optional[str]]:
    try:
        c = int(code) if code is not None else 0
    except (TypeError, ValueError):
        c = 0
    if c == 2:
        return ("error", message or "error")
    return ("ok", None)


# ---- the assembled span ---------------------------------------------------


def _resolve_agent_identity(
    name: Optional[str],
    parent_span_id_hex: Optional[str],
    service_name_hint: Optional[str],
) -> Optional[str]:
    """Collapse multi-phase root span names under one agent identity.

    Lumin's agent grid groups by ``trace.name`` (the root span's name).
    Frameworks like OpenClaw emit several distinct root span names per
    user interaction — ``openclaw.run``, ``openclaw.message.processed``,
    ``openclaw.harness.run``, ``openclaw.context.assembled``,
    ``openclaw.liveness.warning``, ``openclaw.diagnostic.phase`` — each
    landing as a separate trace_id. The unmodified grid then shows
    five-plus ``agent`` cards for what operators rightly think of as
    one agent (the bot itself).

    Rule: when a ROOT span's name starts with a known framework
    prefix, fold to the resource ``service.name`` (which OTel SDKs
    auto-set from ``OTEL_SERVICE_NAME`` or the binary name) — falling
    back to the prefix itself if no service name is present. Children
    keep their original names so the span timeline still shows the
    phase breakdown.

    Today this only kicks in for the ``openclaw.`` prefix. Other
    frameworks haven't shown the same multi-root-name pattern in
    practice (Mastra and VoltAgent emit one root per interaction with
    a user-defined name); add new prefixes here as we encounter them.
    """
    if parent_span_id_hex:
        return name
    if not name:
        return name
    if name.startswith("openclaw."):
        return service_name_hint or "openclaw"
    return name


def _build_span_input(
    *,
    trace_id_hex: str,
    span_id_hex: str,
    parent_span_id_hex: Optional[str],
    name: Optional[str],
    kind: int,
    started_at: Optional[str],
    ended_at: Optional[str],
    status_code: Any,
    status_message: Optional[str],
    attrs: Dict[str, Any],
    service_name_hint: Optional[str] = None,
) -> Optional[SpanInput]:
    """Materialize a Lumin SpanInput from already-normalized OTLP fields.

    Returns None when essentials are missing — namely span_id or
    started_at. Calling code logs and skips so the rest of the batch
    still flows; OTel implementations have shipped malformed spans
    before, and Rule 7 generalized says we never reject a whole
    batch over one bad row.

    ``service_name_hint`` is the resource ``service.name`` from the
    OTLP payload — used to collapse OpenClaw's multiple internal
    root span names under a single agent identity in the grid.
    """
    if not span_id_hex or not started_at:
        return None

    span_id = _hex_to_uuid(span_id_hex)
    trace_id = _hex_to_uuid(trace_id_hex) if trace_id_hex else span_id
    parent_id = _hex_to_uuid(parent_span_id_hex) if parent_span_id_hex else None

    # Read the OTel GenAI semconv keys first; fall back to OpenClaw's
    # native namespace when the strict-OTel attributes aren't set.
    # Real-world: OpenClaw's diagnostics-otel emits both — but other
    # tools (and older OpenClaw builds) may emit only one or the other.
    model = (
        attrs.get("gen_ai.response.model")
        or attrs.get("gen_ai.request.model")
        or attrs.get("openclaw.model")
    )
    provider = (
        attrs.get("gen_ai.system")
        or attrs.get("openclaw.provider")
    )
    tokens_in = attrs.get("gen_ai.usage.input_tokens")
    tokens_out = attrs.get("gen_ai.usage.output_tokens")
    operation = attrs.get("gen_ai.operation.name")

    # Coerce token counts — OTLP may serialize int64 as string (proto3
    # JSON mapping). _jsonvalue_to_python already int-casts, but
    # double-check in case some emitter ships them as strings.
    if tokens_in is not None:
        try:
            tokens_in = int(tokens_in)
        except (TypeError, ValueError):
            tokens_in = None
    if tokens_out is not None:
        try:
            tokens_out = int(tokens_out)
        except (TypeError, ValueError):
            tokens_out = None

    cost = _compute_cost(
        str(model) if model else None,
        tokens_in,
        tokens_out,
    )
    # Span name flows into the classifier so name-prefix patterns
    # ('openclaw.model.*' → llm) work when SpanKind isn't CLIENT.
    # Use the ORIGINAL name here, before the agent-identity fold,
    # so model.call spans are still classified llm even when their
    # root parent gets renamed.
    span_type = _classify_type(kind, attrs, name=name)
    status, error_message = _status_from_otel(status_code, status_message)

    # Span name: OTel's span.name wins. Fall back to the gen_ai.operation.name
    # when name is empty or generic ("span"). Operators usually want
    # something human-readable in the trace list.
    resolved_name = name or operation
    if resolved_name in (None, "", "span") and operation:
        resolved_name = operation

    # Agent-identity fold for root spans (see _resolve_agent_identity).
    # When this rewrites the name, stash the original under a metadata
    # key so the span detail page can still surface what phase this was.
    folded = _resolve_agent_identity(
        resolved_name, parent_span_id_hex, service_name_hint
    )
    if folded != resolved_name and resolved_name:
        attrs = {**attrs, "openclaw.span.original_name": resolved_name}
        resolved_name = folded

    tool_name = (
        attrs.get("gen_ai.tool.name")
        or attrs.get("tool.name")
        or attrs.get("openclaw.tool.name")
    )

    session_id = _extract_session_id(attrs)

    return SpanInput(
        id=span_id,
        trace_id=trace_id,
        parent_span_id=parent_id,
        name=resolved_name,
        type=span_type,
        input=_extract_input(attrs),
        output=_extract_output(attrs),
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        error_message=error_message,
        model=str(model) if model is not None else None,
        provider=str(provider) if provider is not None else None,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        cost_usd=cost,
        tool_name=str(tool_name) if tool_name else None,
        session_id=session_id,
        metadata=_build_metadata(attrs),
    )


# ---- public entry points --------------------------------------------------


def parse_otlp_proto(export_request: Any) -> Tuple[List[SpanInput], Optional[str]]:
    """Decode an ``ExportTraceServiceRequest`` protobuf message into
    a list of Lumin spans.

    Returns ``(spans, service_name_hint)`` — the hint is the resource's
    ``service.name`` attribute, used by the router as a fallback for
    project resolution when no ``X-Lumin-Project`` header is present.
    Pulled from the FIRST resource in the batch (OTel sends one
    resource per emitter; multi-resource batches are rare).
    """
    spans_out: List[SpanInput] = []
    service_name: Optional[str] = None

    for resource_spans in export_request.resource_spans:
        resource_attrs = _attrs_from_proto(resource_spans.resource.attributes)
        # Service name is per-resource. The batch-level `service_name`
        # we return is the first one encountered (used by the router as
        # a project hint); each span gets folded against ITS resource's
        # service name though, so multi-resource batches stay correct.
        resource_service_name = resource_attrs.get("service.name")
        if resource_service_name:
            resource_service_name = str(resource_service_name)
        if service_name is None and resource_service_name:
            service_name = resource_service_name

        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                attrs = _attrs_from_proto(span.attributes)

                trace_id_hex = span.trace_id.hex() if span.trace_id else ""
                span_id_hex = span.span_id.hex() if span.span_id else ""
                parent_id_hex = span.parent_span_id.hex() if span.parent_span_id else None
                if parent_id_hex == "":
                    # Empty bytes → no parent. Don't propagate empty string.
                    parent_id_hex = None

                started_at = _nanos_to_iso(span.start_time_unix_nano)
                ended_at = _nanos_to_iso(span.end_time_unix_nano)

                status_code = span.status.code if span.status else 0
                status_message = (
                    span.status.message if span.status and span.status.message else None
                )

                si = _build_span_input(
                    trace_id_hex=trace_id_hex,
                    span_id_hex=span_id_hex,
                    parent_span_id_hex=parent_id_hex,
                    name=span.name,
                    kind=span.kind,
                    started_at=started_at,
                    ended_at=ended_at,
                    status_code=status_code,
                    status_message=status_message,
                    attrs=attrs,
                    service_name_hint=resource_service_name,
                )
                if si is not None:
                    spans_out.append(si)
                else:
                    logger.warning(
                        "otlp: skipping malformed proto span (id=%r start=%r)",
                        span_id_hex, span.start_time_unix_nano,
                    )

    return spans_out, service_name


def parse_otlp_json(payload: dict) -> Tuple[List[SpanInput], Optional[str]]:
    """OTLP/HTTP JSON variant. Same return shape as the proto path.

    Both shapes are spec'd in
    https://opentelemetry.io/docs/specs/otlp/#otlphttp-request — the
    key differences are:

      - Trace/span IDs are hex strings (already in the format we want)
      - int64 values come over the wire as strings (per proto3 → JSON)
      - Attribute oneof uses ``stringValue`` / ``intValue`` / etc. keys
    """
    spans_out: List[SpanInput] = []
    service_name: Optional[str] = None

    for resource_spans in payload.get("resourceSpans", []) or []:
        resource = resource_spans.get("resource") or {}
        resource_attrs = _attrs_from_json(resource.get("attributes", []))
        resource_service_name = resource_attrs.get("service.name")
        if resource_service_name:
            resource_service_name = str(resource_service_name)
        if service_name is None and resource_service_name:
            service_name = resource_service_name

        for scope_spans in resource_spans.get("scopeSpans", []) or []:
            for span in scope_spans.get("spans", []) or []:
                attrs = _attrs_from_json(span.get("attributes", []))

                trace_id_hex = (span.get("traceId") or "").lower()
                span_id_hex = (span.get("spanId") or "").lower()
                parent_id_hex = (span.get("parentSpanId") or "").lower() or None

                started_at = _nanos_to_iso(span.get("startTimeUnixNano"))
                ended_at = _nanos_to_iso(span.get("endTimeUnixNano"))

                status = span.get("status") or {}
                status_code = status.get("code", 0)
                status_message = status.get("message")

                # OTLP/JSON encodes SpanKind as either an enum string
                # ("SPAN_KIND_CLIENT") or an int — handle both.
                kind_raw = span.get("kind", 0)
                if isinstance(kind_raw, str):
                    kind = {
                        "SPAN_KIND_INTERNAL": 1,
                        "SPAN_KIND_SERVER":   2,
                        "SPAN_KIND_CLIENT":   3,
                        "SPAN_KIND_PRODUCER": 4,
                        "SPAN_KIND_CONSUMER": 5,
                    }.get(kind_raw, 0)
                else:
                    try:
                        kind = int(kind_raw)
                    except (TypeError, ValueError):
                        kind = 0

                si = _build_span_input(
                    trace_id_hex=trace_id_hex,
                    span_id_hex=span_id_hex,
                    parent_span_id_hex=parent_id_hex,
                    name=span.get("name"),
                    kind=kind,
                    started_at=started_at,
                    ended_at=ended_at,
                    status_code=status_code,
                    status_message=status_message,
                    attrs=attrs,
                    service_name_hint=resource_service_name,
                )
                if si is not None:
                    spans_out.append(si)
                else:
                    logger.warning(
                        "otlp: skipping malformed JSON span (id=%r)", span_id_hex
                    )

    return spans_out, service_name


# ---- project resolution ---------------------------------------------------


# Mirror the ingest allowlist used by spans.ingest_spans. Closed-set
# project values keep the agent grid's "framework" headlines stable —
# any other value (the OTLP service.name from a third-party tool,
# typically) folds to "default". Operators that want a dedicated
# section should ship a Lumin SDK package, not an arbitrary header.
_ALLOWED_PROJECTS = frozenset({"openclaw", "mastra", "voltagent", "default"})


def normalize_project(
    header_value: Optional[str], service_name_hint: Optional[str]
) -> Optional[str]:
    """Resolve the ``project`` field for ingested OTLP spans.

    Priority:
      1. ``X-Lumin-Project`` HTTP header
      2. Resource attribute ``service.name``
      3. None (downstream coalesces to "default" at read time)

    Whichever wins is then folded through the closed-set allowlist —
    matches what spans.ingest_spans does for the native /v1/spans
    path. Same headlines, no surprise framework sections.
    """
    raw = (header_value or "").strip() or (service_name_hint or "").strip()
    if not raw:
        return None
    v = raw.lower()
    if v in _ALLOWED_PROJECTS:
        return v
    return "default"
