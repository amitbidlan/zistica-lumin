"""Lumin LLM Proxy — OpenAI-compatible passthrough that captures spans.

This is the second of Lumin's two ingest rails:

  1. OTLP receiver (``/v1/otlp/v1/traces``) — passive: any OTel-emitting
     framework can post `gen_ai.*` spans to us, no client changes.
  2. **This module** — active: the agent points its OpenAI / Ollama /
     Anthropic-compat client base URL at ``/v1/openai`` and we proxy to
     the real upstream while observing the request and response.

The proxy is the only path that is *guaranteed* to see content, even
when the upstream framework redacts. It also gives us a join point with
OTel: if the caller passes a W3C ``traceparent`` header (OpenClaw will
once the upstream PR in Step 4 lands), the proxy reuses the same
``trace_id`` so a chat completion span fuses into the same trace as the
OTel run that drove it. Operators see one timeline, two ingest rails.

The router is *intentionally* a thin transport. All the per-provider
parsing logic — pricing, message extraction, usage shape — already
lives in ``otlp_decode``, so we import the pieces we need rather than
re-implementing them. Anything new (e.g. Anthropic's Messages API
shape) belongs in a small helper here, NOT a fork of otlp_decode.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from db import Database, get_db
from models import SpanInput
from otlp_decode import _compute_cost
from routers.spans import (
    _broadcast_after_insert,
    _evaluate_policies_for_batch,
    _insert_span,
    _normalize_project,
    _upsert_trace_from_span,
)

logger = logging.getLogger("lumin.api.proxy")

router = APIRouter()


# --- config ---------------------------------------------------------------


def _default_upstream_base() -> str:
    """OpenAI-compat upstream. Operators can swap this to point at
    Ollama (``http://localhost:11434``), an Anthropic-compat gateway,
    or any other OpenAI-shaped endpoint. Trailing slashes stripped so
    URL composition is deterministic.
    """
    return os.environ.get("LUMIN_PROXY_OPENAI_BASE", "https://api.openai.com").rstrip("/")


def _request_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get("LUMIN_PROXY_TIMEOUT_S", "300")))
    except ValueError:
        return 300.0


# Headers we never forward upstream. ``host`` would point at us; the
# others are connection-scoped and break HTTP/1.1 semantics if relayed.
# ``x-lumin-*`` are ours and meaningless to upstreams.
_HOP_BY_HOP = frozenset({
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
})

_LUMIN_PREFIX = "x-lumin-"


# --- traceparent parsing --------------------------------------------------


_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$"
)


def _parse_traceparent(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (trace_id_uuid, parent_span_id_uuid) or (None, None).

    W3C traceparent: ``00-<32 hex trace>-<16 hex span>-<flags>``.
    We reformat the trace half as a UUID and the span half as a 32-hex
    UUID by left-padding so the IDs slot into Lumin's existing trace
    schema and join cleanly with OTLP-ingested spans for the same run.
    """
    if not value:
        return None, None
    m = _TRACEPARENT_RE.match(value.strip())
    if not m:
        return None, None
    trace_hex, span_hex = m.group(1), m.group(2)
    trace_uuid = (
        f"{trace_hex[0:8]}-{trace_hex[8:12]}-{trace_hex[12:16]}-"
        f"{trace_hex[16:20]}-{trace_hex[20:32]}"
    )
    padded = span_hex.rjust(32, "0")
    span_uuid = (
        f"{padded[0:8]}-{padded[8:12]}-{padded[12:16]}-"
        f"{padded[16:20]}-{padded[20:32]}"
    )
    return trace_uuid, span_uuid


# --- header sanitation ----------------------------------------------------


def _filter_request_headers(
    headers: Dict[str, str], upstream_host: str
) -> Dict[str, str]:
    """Strip hop-by-hop and Lumin-internal headers; rewrite Host."""
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk.startswith(_LUMIN_PREFIX):
            continue
        out[k] = v
    out["host"] = upstream_host
    return out


def _filter_response_headers(headers: httpx.Headers) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


# --- request / response shape extraction ----------------------------------
#
# The proxy is OpenAI-compatible, so we know the request schema. We
# only extract what's needed for the span: input messages, model, and
# the streaming hint. Everything else passes through untouched.


def _extract_request_facts(body_bytes: bytes) -> Tuple[Optional[str], Optional[str], bool]:
    """Return (model, input_text, is_streaming). Tolerates non-JSON bodies."""
    if not body_bytes:
        return None, None, False
    try:
        data = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, False
    if not isinstance(data, dict):
        return None, None, False
    model = data.get("model") if isinstance(data.get("model"), str) else None
    is_stream = bool(data.get("stream"))
    # Prefer Chat Completions ``messages``; fall back to legacy
    # Completions ``prompt``; otherwise stringify the whole body so an
    # operator at least sees what was sent.
    input_text: Optional[str]
    msgs = data.get("messages")
    if isinstance(msgs, list):
        input_text = json.dumps(msgs, ensure_ascii=False)
    elif isinstance(data.get("prompt"), str):
        input_text = data["prompt"]
    elif "prompt" in data:
        input_text = json.dumps(data["prompt"], ensure_ascii=False)
    else:
        input_text = json.dumps(data, ensure_ascii=False)
    return model, input_text, is_stream


def _extract_nonstream_response(
    body_bytes: bytes,
) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """Return (output_text, prompt_tokens, completion_tokens) from a
    JSON OpenAI-compat response body. Handles both Chat Completions
    (``choices[].message.content``) and the older Completions shape
    (``choices[].text``)."""
    if not body_bytes:
        return None, None, None
    try:
        data = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    output_text: Optional[str] = None
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    output_text = content
                elif content is not None:
                    output_text = json.dumps(content, ensure_ascii=False)
            elif isinstance(first.get("text"), str):
                output_text = first["text"]
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    pt = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None
    ct = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None
    return output_text, pt, ct


def _accumulate_sse_stream(
    chunks: List[bytes],
) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """Reassemble an OpenAI-style SSE chat-completions stream into the
    final assistant message + usage. Lines look like
    ``data: {"choices":[{"delta":{"content":"..."}}]}`` plus a final
    ``data: [DONE]``. We concatenate every ``delta.content`` piece and
    pick up ``usage`` from the optional last data event when the
    upstream supports ``stream_options.include_usage``.

    Tolerant: malformed lines are skipped, never raised. The proxy must
    not destabilize the agent because Lumin couldn't parse a chunk.
    """
    out_parts: List[str] = []
    pt: Optional[int] = None
    ct: Optional[int] = None
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None, None, None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        choices = evt.get("choices")
        if isinstance(choices, list):
            for c in choices:
                if not isinstance(c, dict):
                    continue
                delta = c.get("delta")
                if isinstance(delta, dict):
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        out_parts.append(piece)
                # Some servers send the final accumulated message under
                # ``message`` instead of ``delta`` on the closing chunk.
                msg = c.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    out_parts.append(msg["content"])
        usage = evt.get("usage") if isinstance(evt.get("usage"), dict) else None
        if usage:
            if isinstance(usage.get("prompt_tokens"), int):
                pt = usage["prompt_tokens"]
            if isinstance(usage.get("completion_tokens"), int):
                ct = usage["completion_tokens"]
    output = "".join(out_parts) if out_parts else None
    return output, pt, ct


# --- span construction ----------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _provider_from_upstream(base_url: str) -> str:
    """Friendly provider label inferred from the upstream host. Used
    for the agent grid; falls back to the bare host so unknown
    gateways still get a recognizable name."""
    host = (urlparse(base_url).hostname or "").lower()
    if "openai" in host:
        return "openai"
    if "anthropic" in host:
        return "anthropic"
    if "ollama" in host or host in ("localhost", "127.0.0.1"):
        return "ollama"
    if "mistral" in host:
        return "mistral"
    if "groq" in host:
        return "groq"
    return host or "unknown"


def _build_span(
    *,
    path: str,
    started_at_iso: str,
    ended_at_iso: str,
    status_code: int,
    upstream_base: str,
    model: Optional[str],
    input_text: Optional[str],
    output_text: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    trace_id: Optional[str],
    parent_span_id: Optional[str],
    session_id: Optional[str],
    error_message: Optional[str],
) -> SpanInput:
    """Compose a SpanInput from observed proxy data. Uses the same
    pricing table as the OTLP rail so cost numbers stay consistent
    regardless of which path produced the span.
    """
    span_id = str(uuid.uuid4())
    final_trace_id = trace_id or span_id
    cost = _compute_cost(model, prompt_tokens, completion_tokens)
    status = "ok" if 200 <= status_code < 300 else "error"
    if error_message is None and status == "error":
        error_message = f"upstream returned HTTP {status_code}"

    name = "lumin.proxy.completion"
    if path.endswith("chat/completions"):
        name = "openai.chat.completion"
    elif path.endswith("completions"):
        name = "openai.completion"
    elif path.endswith("embeddings"):
        name = "openai.embedding"
    elif path.endswith("messages"):
        name = "anthropic.message"

    return SpanInput(
        id=span_id,
        trace_id=final_trace_id,
        parent_span_id=parent_span_id,
        name=name,
        type="llm",
        started_at=started_at_iso,
        ended_at=ended_at_iso,
        status=status,
        error_message=error_message,
        model=model,
        provider=_provider_from_upstream(upstream_base),
        tokens_input=prompt_tokens,
        tokens_output=completion_tokens,
        cost_usd=cost,
        input=input_text,
        output=output_text,
        session_id=session_id,
        metadata={
            "lumin.proxy.upstream": upstream_base,
            "lumin.proxy.path": path,
            "lumin.proxy.http_status": status_code,
        },
    )


def _persist_span(
    db: Database, span: SpanInput, project: Optional[str]
) -> bool:
    """Same hot path as ``POST /v1/spans``: insert, upsert the parent
    trace, and return the ``is_new_trace`` flag for the broadcaster."""
    _insert_span(db, span, project=project)
    return _upsert_trace_from_span(db, span, project=project)


# --- the proxy endpoint ---------------------------------------------------


@router.post("/v1/openai/{path:path}")
async def openai_proxy(
    path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    x_lumin_project: Optional[str] = Header(default=None),
    x_lumin_upstream: Optional[str] = Header(default=None),
    x_lumin_session: Optional[str] = Header(default=None),
    traceparent: Optional[str] = Header(default=None),
):
    """Forward an OpenAI-compatible request to the configured upstream
    and capture a Lumin span on the way back.

    Path is preserved verbatim under the upstream base, so a client
    pointed at ``http://localhost:8000/v1/openai`` sees the upstream
    surface unchanged: ``/v1/chat/completions``, ``/v1/embeddings``,
    ``/v1/models``, etc. all work.

    Streaming responses (``stream: true``) are passed through chunk
    by chunk; we buffer in parallel to assemble the span at the end.
    Non-streaming responses are returned in a single Response.

    Errors from the upstream are returned as-is; a span is still
    emitted so operators can see failed calls in the dashboard.
    """
    upstream_base = (x_lumin_upstream or _default_upstream_base()).rstrip("/")
    target_url = f"{upstream_base}/{path.lstrip('/')}"
    upstream_host = urlparse(upstream_base).netloc

    body_bytes = await request.body()
    fwd_headers = _filter_request_headers(dict(request.headers), upstream_host)
    # Preserve query string verbatim — OpenAI doesn't use it but
    # Anthropic / Azure forks sometimes do (e.g. api-version).
    qs = request.url.query

    project = _normalize_project(x_lumin_project)
    trace_id, parent_span_id = _parse_traceparent(traceparent)
    started_at = _utc_now_iso()
    started_mono = time.monotonic()

    model, input_text, is_stream = _extract_request_facts(body_bytes)

    timeout = httpx.Timeout(_request_timeout_s(), connect=15.0)
    client = httpx.AsyncClient(timeout=timeout)

    if is_stream:
        # Stream pass-through. We open the upstream stream, forward
        # bytes as they arrive, and accumulate them locally to assemble
        # the span when the stream closes. The accumulator is a list
        # of bytes (no concatenation per chunk → no O(N^2) cost).
        try:
            req = client.build_request(
                "POST",
                target_url,
                headers=fwd_headers,
                content=body_bytes,
                params=qs or None,
            )
            upstream_resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            ended_at = _utc_now_iso()
            span = _build_span(
                path=path,
                started_at_iso=started_at,
                ended_at_iso=ended_at,
                status_code=502,
                upstream_base=upstream_base,
                model=model,
                input_text=input_text,
                output_text=None,
                prompt_tokens=None,
                completion_tokens=None,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                session_id=x_lumin_session,
                error_message=f"upstream connection failed: {exc}",
            )
            is_new = _persist_span(db, span, project)
            _broadcast_after_insert(db, span, is_new)
            background_tasks.add_task(_evaluate_policies_for_batch, db, [span])
            return JSONResponse(
                status_code=502,
                content={"error": {"message": str(exc), "type": "upstream_unreachable"}},
            )

        captured: List[bytes] = []

        async def _stream_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_resp.aiter_raw():
                    captured.append(chunk)
                    yield chunk
            finally:
                # Ensure we always close upstream + assemble the span
                # even if the client disconnected mid-stream.
                try:
                    await upstream_resp.aclose()
                finally:
                    await client.aclose()
                ended_at = _utc_now_iso()
                output_text, pt, ct = _accumulate_sse_stream(captured)
                span = _build_span(
                    path=path,
                    started_at_iso=started_at,
                    ended_at_iso=ended_at,
                    status_code=upstream_resp.status_code,
                    upstream_base=upstream_base,
                    model=model,
                    input_text=input_text,
                    output_text=output_text,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    trace_id=trace_id,
                    parent_span_id=parent_span_id,
                    session_id=x_lumin_session,
                    error_message=None,
                )
                try:
                    is_new = _persist_span(db, span, project)
                    _broadcast_after_insert(db, span, is_new)
                except Exception:
                    logger.exception("proxy: failed to persist streaming span")
                # Policy eval after the stream completes — same pattern
                # as the synchronous /v1/spans path.
                try:
                    _evaluate_policies_for_batch(db, [span])
                except Exception:
                    logger.exception("proxy: policy eval failed")
                # NB: elapsed kept for log diagnostics
                _ = time.monotonic() - started_mono

        return StreamingResponse(
            _stream_iter(),
            status_code=upstream_resp.status_code,
            headers=_filter_response_headers(upstream_resp.headers),
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        )

    # Non-streaming path. Single round trip; we have the whole body.
    try:
        upstream_resp = await client.post(
            target_url,
            headers=fwd_headers,
            content=body_bytes,
            params=qs or None,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        ended_at = _utc_now_iso()
        span = _build_span(
            path=path,
            started_at_iso=started_at,
            ended_at_iso=ended_at,
            status_code=502,
            upstream_base=upstream_base,
            model=model,
            input_text=input_text,
            output_text=None,
            prompt_tokens=None,
            completion_tokens=None,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            session_id=x_lumin_session,
            error_message=f"upstream connection failed: {exc}",
        )
        is_new = _persist_span(db, span, project)
        _broadcast_after_insert(db, span, is_new)
        background_tasks.add_task(_evaluate_policies_for_batch, db, [span])
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_unreachable"}},
        )
    finally:
        await client.aclose()

    resp_bytes = upstream_resp.content
    ended_at = _utc_now_iso()
    output_text, pt, ct = _extract_nonstream_response(resp_bytes)
    err: Optional[str] = None
    if upstream_resp.status_code >= 400:
        try:
            err_data = json.loads(resp_bytes)
            if isinstance(err_data, dict):
                # OpenAI: {"error": {"message": "..."}}
                err_obj = err_data.get("error")
                if isinstance(err_obj, dict) and isinstance(err_obj.get("message"), str):
                    err = err_obj["message"]
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    span = _build_span(
        path=path,
        started_at_iso=started_at,
        ended_at_iso=ended_at,
        status_code=upstream_resp.status_code,
        upstream_base=upstream_base,
        model=model,
        input_text=input_text,
        output_text=output_text,
        prompt_tokens=pt,
        completion_tokens=ct,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        session_id=x_lumin_session,
        error_message=err,
    )
    is_new = _persist_span(db, span, project)
    _broadcast_after_insert(db, span, is_new)
    background_tasks.add_task(_evaluate_policies_for_batch, db, [span])

    return Response(
        content=resp_bytes,
        status_code=upstream_resp.status_code,
        headers=_filter_response_headers(upstream_resp.headers),
        media_type=upstream_resp.headers.get("content-type"),
    )
