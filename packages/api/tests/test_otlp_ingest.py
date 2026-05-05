"""Tests for the OTLP/HTTP ingest endpoint POST /v1/otlp/v1/traces.

Covers both wire formats (protobuf + JSON), gen_ai.* attribute
mapping, cost calculation, project resolution (header vs
service.name), trace upsert from a root span, child-before-root
stub creation, policy-engine triggering, and the negative paths
(unknown content-type, invalid bytes).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest


# --- helpers ----------------------------------------------------------------


def _hex_id(n: int) -> str:
    """Random hex string of length n. Used for trace_id (32) + span_id (16)."""
    return uuid.uuid4().hex[: n // 2] + uuid.uuid4().hex[: n // 2]


def _now_ns(offset_seconds: int = 0) -> str:
    """Nanoseconds since epoch as a string — OTLP/JSON encodes int64 this way."""
    return str(int((time.time() + offset_seconds) * 1_000_000_000))


def _otlp_json_payload(
    *,
    trace_id: Optional[str] = None,
    spans: Optional[list] = None,
    service_name: Optional[str] = None,
):
    """Minimal OTLP/HTTP JSON envelope. Each span in ``spans`` is a
    plain dict; we wrap with the ResourceSpans/ScopeSpans scaffolding."""
    if trace_id is None:
        trace_id = _hex_id(32)
    if spans is None:
        # Default: one llm span with gen_ai.* attributes
        spans = [{
            "traceId": trace_id,
            "spanId":  _hex_id(16),
            "name":    "chat_completion",
            "kind":    3,  # CLIENT
            "startTimeUnixNano": _now_ns(),
            "endTimeUnixNano":   _now_ns(1),
            "attributes": [
                {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "150"}},
                {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "50"}},
            ],
            "status": {"code": 1},  # OK
        }]
    resource_attrs = []
    if service_name is not None:
        resource_attrs.append(
            {"key": "service.name", "value": {"stringValue": service_name}}
        )
    return {
        "resourceSpans": [{
            "resource": {"attributes": resource_attrs},
            "scopeSpans": [{"spans": spans}],
        }]
    }


def _post_json(client, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return client.post("/v1/otlp/v1/traces", content=json.dumps(payload), headers=h)


def _build_otlp_proto(json_payload: dict) -> bytes:
    """Convert a JSON-shaped payload into a real protobuf payload.

    Building this from the bottom up rather than via ``json_format.ParseDict``
    because the latter treats protobuf ``bytes`` fields as base64-encoded
    (per the proto3 JSON canonical mapping), but OTLP/JSON uses hex
    for trace_id / span_id. So we wire the proto directly — converting
    hex → bytes ourselves.
    """
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    from opentelemetry.proto.trace.v1 import trace_pb2
    from opentelemetry.proto.common.v1 import common_pb2
    from opentelemetry.proto.resource.v1 import resource_pb2

    def _attr(kv: dict) -> common_pb2.KeyValue:
        v = kv["value"]
        any_value = common_pb2.AnyValue()
        if "stringValue" in v:
            any_value.string_value = v["stringValue"]
        elif "intValue" in v:
            any_value.int_value = int(v["intValue"])
        elif "boolValue" in v:
            any_value.bool_value = bool(v["boolValue"])
        elif "doubleValue" in v:
            any_value.double_value = float(v["doubleValue"])
        return common_pb2.KeyValue(key=kv["key"], value=any_value)

    req = trace_service_pb2.ExportTraceServiceRequest()
    for rs_in in json_payload.get("resourceSpans", []):
        rs = req.resource_spans.add()
        res_attrs = rs_in.get("resource", {}).get("attributes", [])
        rs.resource.CopyFrom(
            resource_pb2.Resource(attributes=[_attr(a) for a in res_attrs])
        )
        for ss_in in rs_in.get("scopeSpans", []):
            ss = rs.scope_spans.add()
            for s_in in ss_in.get("spans", []):
                s = ss.spans.add()
                if s_in.get("traceId"):
                    s.trace_id = bytes.fromhex(s_in["traceId"])
                if s_in.get("spanId"):
                    s.span_id = bytes.fromhex(s_in["spanId"])
                if s_in.get("parentSpanId"):
                    s.parent_span_id = bytes.fromhex(s_in["parentSpanId"])
                if s_in.get("name"):
                    s.name = s_in["name"]
                if s_in.get("kind") is not None:
                    s.kind = int(s_in["kind"])
                if s_in.get("startTimeUnixNano"):
                    s.start_time_unix_nano = int(s_in["startTimeUnixNano"])
                if s_in.get("endTimeUnixNano"):
                    s.end_time_unix_nano = int(s_in["endTimeUnixNano"])
                for a in s_in.get("attributes", []):
                    s.attributes.add().CopyFrom(_attr(a))
                status = s_in.get("status") or {}
                if status:
                    s.status.CopyFrom(trace_pb2.Status(
                        code=int(status.get("code", 0)),
                        message=status.get("message", ""),
                    ))
    return req.SerializeToString()


# --- 1. JSON happy path -----------------------------------------------------


def test_otlp_json_basic(client):
    """A minimal JSON payload lands a span and surfaces it via /v1/traces."""
    trace_id = _hex_id(32)
    payload = _otlp_json_payload(trace_id=trace_id)

    r = _post_json(client, payload)
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/x-protobuf")
    assert r.content == b""

    # Trace appears in the list
    traces = client.get("/v1/traces").json()
    assert any(
        t["id"].replace("-", "") == trace_id for t in traces
    ), f"trace_id={trace_id} not in list (ids={[t['id'] for t in traces]})"


# --- 2. protobuf happy path -------------------------------------------------


def test_otlp_protobuf_basic(client):
    """Same payload, sent as protobuf instead of JSON."""
    trace_id = _hex_id(32)
    payload = _otlp_json_payload(trace_id=trace_id)
    proto_bytes = _build_otlp_proto(payload)

    r = client.post(
        "/v1/otlp/v1/traces",
        content=proto_bytes,
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 200, r.text
    traces = client.get("/v1/traces").json()
    assert any(t["id"].replace("-", "") == trace_id for t in traces)


# --- 3. gen_ai.* attributes get mapped to the right Lumin fields ------------


def test_gen_ai_attributes_mapped(client):
    trace_id = _hex_id(32)
    span_id = _hex_id(16)
    payload = _otlp_json_payload(trace_id=trace_id, spans=[{
        "traceId": trace_id, "spanId": span_id,
        "name": "chat_completion", "kind": 3,
        "startTimeUnixNano": _now_ns(),
        "endTimeUnixNano":   _now_ns(1),
        "attributes": [
            {"key": "gen_ai.system", "value": {"stringValue": "anthropic"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-4"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "200"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "80"}},
        ],
        "status": {"code": 1},
    }])
    r = _post_json(client, payload)
    assert r.status_code == 200

    # Look up the span via the spans endpoint
    trace_uuid = next(
        t["id"] for t in client.get("/v1/traces").json()
        if t["id"].replace("-", "") == trace_id
    )
    spans = client.get(f"/v1/traces/{trace_uuid}/spans").json()
    assert len(spans) >= 1
    s = spans[0]
    assert s["model"] == "claude-sonnet-4"
    assert s["provider"] == "anthropic"
    assert s["tokens_input"] == 200
    assert s["tokens_output"] == 80
    assert s["type"] == "llm"  # SpanKind=CLIENT + gen_ai.* present


# --- 4. cost calculated for known model -------------------------------------


def test_cost_calculated(client):
    """gpt-4o-mini at $0.15/$0.60 per 1M tokens, 150 in + 50 out:
    (150*0.15 + 50*0.60) / 1_000_000 = (22.5 + 30) / 1M = 5.25e-05"""
    trace_id = _hex_id(32)
    span_id = _hex_id(16)
    payload = _otlp_json_payload(trace_id=trace_id, spans=[{
        "traceId": trace_id, "spanId": span_id,
        "name": "chat_completion", "kind": 3,
        "startTimeUnixNano": _now_ns(),
        "endTimeUnixNano":   _now_ns(1),
        "attributes": [
            {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "150"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "50"}},
        ],
    }])
    _post_json(client, payload)

    trace_uuid = next(
        t["id"] for t in client.get("/v1/traces").json()
        if t["id"].replace("-", "") == trace_id
    )
    spans = client.get(f"/v1/traces/{trace_uuid}/spans").json()
    cost = spans[0]["cost_usd"]
    expected = (150 * 0.15 + 50 * 0.60) / 1_000_000
    assert cost is not None
    assert abs(cost - expected) < 1e-9, f"cost={cost} expected={expected}"


# --- 5. project resolution from X-Lumin-Project header ----------------------


def test_project_from_header(client):
    """Header wins over service.name. Allowlisted value passes through."""
    trace_id = _hex_id(32)
    payload = _otlp_json_payload(trace_id=trace_id, service_name="some-other-service")
    r = _post_json(client, payload, headers={"X-Lumin-Project": "openclaw"})
    assert r.status_code == 200

    body = client.get("/v1/agents").json()
    # The agent's name comes from span.name = "chat_completion"
    a = next((a for a in body["agents"] if a["name"] == "chat_completion"), None)
    assert a is not None
    assert a["project"] == "openclaw"


# --- 6. project resolution falls back to service.name -----------------------


def test_project_from_service_name(client):
    """No header → service.name folds through the allowlist (→ default
    for any non-allowlisted value, mirrors the /v1/spans path)."""
    trace_id = _hex_id(32)
    payload = _otlp_json_payload(trace_id=trace_id, service_name="my-agent")
    r = _post_json(client, payload)
    assert r.status_code == 200

    body = client.get("/v1/agents").json()
    a = next((a for a in body["agents"] if a["name"] == "chat_completion"), None)
    assert a is not None
    # "my-agent" isn't in the allowlist → folds to "default"
    assert a["project"] == "default"


# --- 7. root span creates the trace -----------------------------------------


def test_trace_created_from_root_span(client):
    """A root span (no parentSpanId) lands a trace row visible at
    /v1/traces/{id} with name + started_at + ended_at populated."""
    trace_id = _hex_id(32)
    span_id = _hex_id(16)
    payload = _otlp_json_payload(trace_id=trace_id, spans=[{
        "traceId": trace_id, "spanId": span_id,
        "name": "agent.run", "kind": 3,
        "startTimeUnixNano": _now_ns(),
        "endTimeUnixNano":   _now_ns(2),
        "attributes": [],
    }])
    _post_json(client, payload)

    traces = client.get("/v1/traces").json()
    t = next(t for t in traces if t["id"].replace("-", "") == trace_id)
    assert t["name"] == "agent.run"
    assert t["started_at"] is not None
    assert t["ended_at"] is not None


# --- 8. child spans attach to the right trace ------------------------------


def test_child_spans_attached(client):
    """Root + 2 children share trace_id; spans endpoint returns all 3."""
    trace_id = _hex_id(32)
    root_id = _hex_id(16)
    child1 = _hex_id(16)
    child2 = _hex_id(16)
    payload = _otlp_json_payload(trace_id=trace_id, spans=[
        {
            "traceId": trace_id, "spanId": root_id,
            "name": "agent.run", "kind": 3,
            "startTimeUnixNano": _now_ns(0),
            "endTimeUnixNano":   _now_ns(3),
            "attributes": [],
        },
        {
            "traceId": trace_id, "spanId": child1, "parentSpanId": root_id,
            "name": "step.search", "kind": 3,
            "startTimeUnixNano": _now_ns(0),
            "endTimeUnixNano":   _now_ns(1),
            "attributes": [{"key": "tool.name", "value": {"stringValue": "search"}}],
        },
        {
            "traceId": trace_id, "spanId": child2, "parentSpanId": root_id,
            "name": "step.summarize", "kind": 3,
            "startTimeUnixNano": _now_ns(1),
            "endTimeUnixNano":   _now_ns(2),
            "attributes": [
                {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1000"}},
                {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "200"}},
            ],
        },
    ])
    r = _post_json(client, payload)
    assert r.status_code == 200

    trace_uuid = next(
        t["id"] for t in client.get("/v1/traces").json()
        if t["id"].replace("-", "") == trace_id
    )
    spans = client.get(f"/v1/traces/{trace_uuid}/spans").json()
    assert len(spans) == 3

    # Type detection: tool.name → "tool"; gen_ai.* + CLIENT → "llm"
    by_name = {s["name"]: s for s in spans}
    assert by_name["step.search"]["type"] == "tool"
    assert by_name["step.summarize"]["type"] == "llm"


# --- 9. policy engine fires on OTLP-ingested spans -------------------------


def test_policy_fires_on_otlp_span(client, db, tmp_path: Path):
    """Same policy-runtime path that /v1/spans exercises must trigger
    on an OTLP-shaped span. Critical for the wedge — operators using
    Logfire/Phoenix/Datadog get policy enforcement for free."""
    import os
    import policy_runtime

    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text("""
version: 1
policies:
  - name: otlp_smoke_policy
    description: Fires on any LLM span
    trigger: span_end
    condition: "span.type == 'llm'"
    action: flag
    severity: low
""", encoding="utf-8")

    old = os.environ.get("LUMIN_POLICY_FILE")
    os.environ["LUMIN_POLICY_FILE"] = str(yaml_path)
    policy_runtime._reset_for_tests()
    try:
        trace_id = _hex_id(32)
        payload = _otlp_json_payload(trace_id=trace_id)
        _post_json(client, payload)

        trace_uuid = next(
            t["id"] for t in client.get("/v1/traces").json()
            if t["id"].replace("-", "") == trace_id
        )
        listing = client.get(f"/v1/violations?trace_id={trace_uuid}").json()
        names = {v["policy_name"] for v in listing["violations"]}
        assert "otlp_smoke_policy" in names, (
            f"expected otlp_smoke_policy to fire; got {names}"
        )
    finally:
        policy_runtime._reset_for_tests()
        if old is None:
            os.environ.pop("LUMIN_POLICY_FILE", None)
        else:
            os.environ["LUMIN_POLICY_FILE"] = old


# --- 10. unknown content-type tries protobuf -------------------------------


def test_unknown_content_type_tries_protobuf(client):
    """No Content-Type → fall back to protobuf decode. OTel collectors
    occasionally drop the header; we should still ingest cleanly."""
    payload = _otlp_json_payload()
    proto_bytes = _build_otlp_proto(payload)
    r = client.post("/v1/otlp/v1/traces", content=proto_bytes, headers={})
    assert r.status_code == 200, r.text


# --- 11. invalid payload returns 400 (not 500) -----------------------------


def test_invalid_payload_returns_400(client):
    """Garbage bytes can't be parsed as either format → 400."""
    r = client.post(
        "/v1/otlp/v1/traces",
        content=b"\x00\x01\x02 not a real protobuf or json",
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 400, r.text

    r2 = client.post(
        "/v1/otlp/v1/traces",
        content=b"not json either",
        headers={"Content-Type": "application/json"},
    )
    assert r2.status_code == 400, r2.text


# --- bonus: empty body is acknowledged -------------------------------------


def test_empty_body_is_ok(client):
    """OTel collectors sometimes send empty pings — don't 4xx them."""
    r = client.post(
        "/v1/otlp/v1/traces",
        content=b"",
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 200
