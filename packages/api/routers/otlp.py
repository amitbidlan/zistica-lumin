"""OTLP/HTTP ingest endpoint — POST /v1/otlp/v1/traces.

Accepts OpenTelemetry trace exports in either protobuf or JSON
format and routes them through the same DuckDB write + WebSocket
broadcast + policy evaluation path that POST /v1/spans uses for
SDK-native ingest. The decode + translate logic lives in
``otlp_decode.py``; this module is the thin HTTP transport layer.

Why a separate endpoint instead of extending ``/v1/spans``:

  - OTel tools have a hard expectation about the URL shape
    (``/v1/traces`` for OTLP/HTTP). Reusing ``/v1/spans`` would
    require every operator to override an internal exporter URL.
  - Reading content-type, parsing protobuf, and producing the
    OTLP-shaped success response are concerns that don't apply to
    the SDK-native path.

Per ``Development_Rules.md`` Rule 7: a malformed batch must never
crash the API. We log + skip individual spans that fail to parse,
and return 4xx (not 5xx) for unrecoverable wire-format errors.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response

import otlp_decode
import policy_runtime
from db import Database, get_db
from models import SpanInput
from routers.spans import (
    _broadcast_after_insert,
    _evaluate_policies_for_batch,
    _insert_span,
    _upsert_trace_from_span,
)

logger = logging.getLogger("lumin.api.otlp")

router = APIRouter()


# Stable success body for OTLP/HTTP. The OTLP spec says the response
# body is ``ExportTraceServiceResponse``; an empty (zero-byte) message
# is a valid success. Using bytes() keeps us out of having to import
# the proto class just for the response.
_EMPTY_OTLP_RESPONSE = b""


@router.post("/v1/otlp/v1/traces")
async def receive_otlp_traces(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    x_lumin_project: Optional[str] = Header(default=None),
) -> Response:
    """OTLP/HTTP traces endpoint.

    Content-Type resolution:
      - ``application/x-protobuf`` → protobuf decode (the wire default
        for OTel SDK exporters and the OTel collector).
      - ``application/json`` → JSON decode (manual curl, dashboards,
        easier to inspect during debugging).
      - Anything else → try protobuf first, fall back to a 400 if
        that also fails. OTel SDKs default to protobuf and sometimes
        send no Content-Type at all.

    Project resolution:
      - ``X-Lumin-Project`` header (operator-set; wins)
      - Resource attribute ``service.name`` (auto-populated by every
        OTel SDK from ``OTEL_SERVICE_NAME`` or the program name)
      - Default: NULL (read paths coalesce to "default")

    Both go through ``otlp_decode.normalize_project`` so the closed
    framework allowlist applies — keeps the /agents grid headlines
    stable when third-party tools emit arbitrary service names.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    body = await request.body()
    if not body:
        # Empty bodies aren't a wire-format error per se — the OTel
        # collector occasionally sends empty pings. Acknowledge silently.
        return _empty_response()

    spans, service_hint = _decode(body, content_type)
    project = otlp_decode.normalize_project(x_lumin_project, service_hint)

    if not spans:
        # Decoded successfully but no spans to ingest (e.g. an empty
        # ResourceSpans wrapper). Acknowledge — not an error.
        return _empty_response()

    # Reuse the same insert path as POST /v1/spans. The native ingest
    # already handles trace upsert, child-before-root stub creation,
    # WS broadcast, and idempotent policy evaluation — we don't want a
    # parallel write path that drifts.
    accepted = 0
    for span in spans:
        try:
            _insert_span(db, span, project=project)
            is_new_trace = _upsert_trace_from_span(db, span, project=project)
            _broadcast_after_insert(db, span, is_new_trace)
            accepted += 1
        except Exception:
            # Per Rule 7: never let one malformed span abort the batch.
            # OTel ecosystems ship plenty of edge cases (clock-skewed
            # timestamps, weird hex IDs); soak them silently and move on.
            logger.exception("otlp: failed to insert span %r", span.id)

    if accepted:
        background_tasks.add_task(_evaluate_policies_for_batch, db, list(spans))

    return _empty_response()


def _decode(body: bytes, content_type: str):
    """Parse the OTLP payload into ``SpanInput`` objects.

    Splits content-type matching from the actual decode so the router
    can stay short. Returns ``(spans, service_hint)`` like the
    underlying decoders do.
    """
    if "application/json" in content_type:
        return _decode_json(body)
    if "application/x-protobuf" in content_type or not content_type:
        # Most OTel SDK exporters set ``application/x-protobuf``. A
        # missing content-type is treated as protobuf — we tried JSON
        # there too historically and it confused dashboards that send
        # the OTel collector's default headers.
        return _decode_proto(body)
    # Neither known content-type matched. Final fallback: try protobuf
    # since that's the wire default for OTel.
    return _decode_proto(body)


def _decode_json(body: bytes):
    import json
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"otlp: invalid JSON body: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="otlp: JSON body must be an ExportTraceServiceRequest object",
        )
    try:
        return otlp_decode.parse_otlp_json(payload)
    except Exception as e:
        # Decode bug (not a wire-format issue) — surface as 500 so
        # ops sees it; but log details so we can fix.
        logger.exception("otlp: JSON decode crashed")
        raise HTTPException(status_code=500, detail=f"otlp: decode error: {e}")


def _decode_proto(body: bytes):
    try:
        from opentelemetry.proto.collector.trace.v1 import (
            trace_service_pb2,
        )
    except ImportError as e:
        # opentelemetry-proto missing from the install — surface clearly.
        # Should not happen in normal deploys (it's in requirements.txt)
        # but tests + odd runtime configs occasionally hit this.
        raise HTTPException(
            status_code=500,
            detail=(
                "otlp: opentelemetry-proto is not installed. "
                f"Install it via 'pip install opentelemetry-proto'. ({e})"
            ),
        )
    export_request = trace_service_pb2.ExportTraceServiceRequest()
    try:
        export_request.ParseFromString(body)
    except Exception as e:
        # Includes both google.protobuf.message.DecodeError and any
        # downstream type errors.
        raise HTTPException(
            status_code=400, detail=f"otlp: invalid protobuf body: {e}"
        )
    try:
        return otlp_decode.parse_otlp_proto(export_request)
    except Exception as e:
        logger.exception("otlp: proto decode crashed")
        raise HTTPException(status_code=500, detail=f"otlp: decode error: {e}")


def _empty_response() -> Response:
    """OTLP/HTTP success: empty body, 200 OK, protobuf media type.

    The OTel collector and most SDKs treat any 2xx with an empty body
    as success. Setting the media type to ``application/x-protobuf``
    keeps strict clients happy.
    """
    return Response(
        content=_EMPTY_OTLP_RESPONSE,
        status_code=200,
        media_type="application/x-protobuf",
    )
