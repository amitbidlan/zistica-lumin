"""End-to-end test: real HTTP server receives the SDK's POST."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import lumin
import lumin.sdk as sdk_module
from lumin.config import Config
from lumin.sdk import LuminSDK


class _RecordingHandler(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).received.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body) if body else None,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted": 1}')

    def log_message(self, *args, **kwargs):
        pass  # quiet


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stub_server():
    _RecordingHandler.received = []
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, _RecordingHandler.received
    finally:
        server.shutdown()
        server.server_close()


def test_real_post_hits_v1_spans_endpoint(stub_server):
    port, received = stub_server
    config = Config(
        host=f"http://127.0.0.1:{port}",
        flush_interval=3600.0,
        export_timeout=2.0,
    )
    sdk = LuminSDK(config=config)
    previous = sdk_module._global_sdk
    sdk_module._global_sdk = sdk
    try:

        @lumin.trace
        def my_agent(x: str) -> str:
            return f"hello {x}"

        assert my_agent("test") == "hello test"
        sdk.flush()
    finally:
        sdk_module._global_sdk = previous
        sdk.shutdown()

    assert len(received) == 1, f"expected 1 POST, got {len(received)}"
    request = received[0]
    assert request["path"] == "/v1/spans"
    assert request["headers"].get("Content-Type") == "application/json"

    body = request["body"]
    assert "spans" in body
    assert len(body["spans"]) == 1
    span = body["spans"][0]

    # Required span fields per Session 1 spec
    for field in (
        "id",
        "trace_id",
        "parent_span_id",
        "name",
        "type",
        "input",
        "output",
        "started_at",
        "ended_at",
        "error",
    ):
        assert field in span, f"missing field: {field}"

    assert span["name"] == "my_agent"
    assert span["type"] == "custom"
    assert span["parent_span_id"] is None
    assert span["trace_id"] == span["id"]
    assert span["error"] is None
    assert json.loads(span["input"]) == {"args": ["test"], "kwargs": {}}
    assert json.loads(span["output"]) == "hello test"


def test_atexit_style_flush_via_shutdown(stub_server):
    """Spans submitted right before shutdown must still be POSTed."""
    port, received = stub_server
    config = Config(
        host=f"http://127.0.0.1:{port}",
        flush_interval=3600.0,  # background flusher would never fire
        export_timeout=2.0,
    )
    sdk = LuminSDK(config=config)
    previous = sdk_module._global_sdk
    sdk_module._global_sdk = sdk
    try:

        @lumin.trace
        def quick():
            return 1

        quick()
        # No explicit flush — rely on shutdown to drain.
    finally:
        sdk_module._global_sdk = previous
        sdk.shutdown()

    assert len(received) == 1
    assert received[0]["path"] == "/v1/spans"
