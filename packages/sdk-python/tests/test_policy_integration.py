"""End-to-end tests for the SDK + Policy Engine integration.

These exercise the wiring (configure → engine loaded → spans evaluated
→ violations POSTed → webhook fired). The pure engine unit tests live
in test_policy_engine.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List

import httpx
import pytest

import lumin


def _write_policy(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "policies.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class _MockTransport(httpx.AsyncBaseTransport):
    """Captures every outbound request the dispatcher makes."""

    def __init__(self):
        self.requests: List[httpx.Request] = []
        # Optional crash mode: when True, all requests raise — used to
        # verify the agent doesn't see webhook errors.
        self.fail = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("simulated network failure")
        return httpx.Response(200, json={"accepted": 1})


@pytest.fixture
def patched_dispatcher_client(monkeypatch):
    """Replace the dispatcher's httpx.AsyncClient with a mock that
    records requests, so tests can assert what was sent without
    needing a real Lumin API."""
    transport = _MockTransport()

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=transport, timeout=kwargs.get("timeout", 5.0))

    from lumin import policy_dispatcher
    monkeypatch.setattr(policy_dispatcher.httpx, "AsyncClient", factory)
    yield transport


def _wait_for_requests(transport: _MockTransport, n: int, timeout: float = 2.0) -> None:
    """Poll until the mock transport has captured `n` requests, with timeout."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(transport.requests) >= n:
            return
        time.sleep(0.05)


# --- Policy disabled ---------------------------------------------------------


def test_policy_disabled_when_no_file_configured(patched_dispatcher_client):
    """No policy_file → engine never instantiated → zero overhead."""
    lumin.configure(host="http://localhost:8000")
    sdk = lumin.sdk._get_sdk()
    assert sdk._policy is None

    @lumin.trace
    def f():
        return "ok"

    f()
    sdk.shutdown()
    # Nothing was sent to /v1/violations (mock transport is the
    # *exporter's* client too, but the policy mock isn't the exporter
    # mock — the exporter does still post spans). What we assert is
    # that no /v1/violations POST happened.
    violations_calls = [
        r for r in patched_dispatcher_client.requests
        if str(r.url).endswith("/v1/violations")
    ]
    assert violations_calls == []


def test_invalid_policy_file_disables_engine_cleanly(tmp_path, caplog):
    """Bad YAML → engine disabled, agent still runs, no exception."""
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("policies:\n  - name: missing_required_fields", encoding="utf-8")

    with caplog.at_level("WARNING", logger="lumin.sdk"):
        lumin.configure(
            host="http://localhost:8000",
            policy_file=str(bad_file),
        )
    sdk = lumin.sdk._get_sdk()
    assert sdk._policy is None

    @lumin.trace
    def f():
        return "ok"

    # Agent runs cleanly
    assert f() == "ok"
    sdk.shutdown()


def test_missing_policy_file_disables_engine_cleanly(tmp_path):
    lumin.configure(
        host="http://localhost:8000",
        policy_file=str(tmp_path / "does-not-exist.yaml"),
    )
    sdk = lumin.sdk._get_sdk()
    assert sdk._policy is None
    sdk.shutdown()


# --- Violations posted -------------------------------------------------------


def test_violations_posted_when_policy_fires(tmp_path, patched_dispatcher_client):
    """Span exceeds duration threshold → POST to /v1/violations."""
    f = _write_policy(tmp_path, """
version: 1
policies:
  - name: any_span
    trigger: span_end
    condition: "1 == 1"
    action: flag
    severity: medium
""")
    lumin.configure(
        host="http://localhost:8000",
        policy_file=str(f),
    )
    sdk = lumin.sdk._get_sdk()
    assert sdk._policy is not None

    @lumin.trace
    def agent():
        return "done"

    agent()
    # Wait for the async violations POST to land on the mock transport
    _wait_for_requests(patched_dispatcher_client, n=1)
    sdk.shutdown()

    violations_calls = [
        r for r in patched_dispatcher_client.requests
        if str(r.url).endswith("/v1/violations")
    ]
    assert len(violations_calls) >= 1
    body = violations_calls[0].read().decode()
    assert "any_span" in body


# --- Webhook firing ----------------------------------------------------------


def test_webhook_fired_on_alert(tmp_path, patched_dispatcher_client):
    """action=alert + webhook_url → POST to webhook URL."""
    f = _write_policy(tmp_path, """
version: 1
policies:
  - name: pii_alert
    trigger: span_end
    condition: "1 == 1"
    action: alert
    severity: critical
    webhook_url: "https://hooks.example.com/lumin"
""")
    lumin.configure(host="http://localhost:8000", policy_file=str(f))
    sdk = lumin.sdk._get_sdk()

    @lumin.trace
    def agent():
        return "done"

    agent()
    # Wait for violation post + webhook
    _wait_for_requests(patched_dispatcher_client, n=2, timeout=3.0)
    sdk.shutdown()

    webhook_calls = [
        r for r in patched_dispatcher_client.requests
        if "hooks.example.com" in str(r.url)
    ]
    assert len(webhook_calls) >= 1
    body = webhook_calls[0].read().decode()
    assert "lumin_policy_violation" in body
    assert "pii_alert" in body
    assert "critical" in body


def test_webhook_failure_does_not_crash_agent(tmp_path, patched_dispatcher_client):
    """Webhook URL unreachable → agent continues, no exception bubbles up."""
    patched_dispatcher_client.fail = True

    f = _write_policy(tmp_path, """
version: 1
policies:
  - name: alerting
    trigger: span_end
    condition: "1 == 1"
    action: alert
    severity: high
    webhook_url: "https://will-fail.example.com/x"
""")
    lumin.configure(host="http://localhost:8000", policy_file=str(f))

    @lumin.trace
    def agent():
        return "done"

    # No exception — the failed POST + failed webhook are both swallowed.
    assert agent() == "done"
    lumin.sdk._get_sdk().shutdown()


# --- Flag action does NOT fire webhook --------------------------------------


def test_flag_action_does_not_fire_webhook(tmp_path, patched_dispatcher_client):
    """Only ``alert`` action triggers a webhook. ``flag`` is DB-only."""
    f = _write_policy(tmp_path, """
version: 1
policies:
  - name: flagged_only
    trigger: span_end
    condition: "1 == 1"
    action: flag
    severity: low
    webhook_url: "https://should-not-fire.example.com"
""")
    lumin.configure(host="http://localhost:8000", policy_file=str(f))

    @lumin.trace
    def agent():
        return "done"

    agent()
    # Allow time for the dispatcher to process — but webhook should never be called
    _wait_for_requests(patched_dispatcher_client, n=1, timeout=1.5)
    lumin.sdk._get_sdk().shutdown()

    webhook_calls = [
        r for r in patched_dispatcher_client.requests
        if "should-not-fire" in str(r.url)
    ]
    assert webhook_calls == []
