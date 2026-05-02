import asyncio

import pytest

import synaptic
import synaptic.sdk as sdk_module
from synaptic.config import Config
from synaptic.exporter import HTTPExporter
from synaptic.queue import BoundedQueue
from synaptic.sdk import SynapticSDK
from synaptic.span import Span


def test_bounded_queue_drops_on_overflow():
    q = BoundedQueue(max_size=3)
    spans = [Span.create(f"s{i}") for i in range(5)]
    results = [q.put_nowait(s) for s in spans]
    assert results == [True, True, True, False, False]
    assert len(q) == 3
    assert q.dropped == 2


def test_failing_exporter_does_not_propagate_to_agent():
    class FailingExporter:
        async def export(self, spans):
            raise RuntimeError("network down")

        async def close(self):
            pass

    config = Config(host="http://test", flush_interval=3600.0, export_timeout=0.5)
    sdk = SynapticSDK(config=config, exporter=FailingExporter())
    previous = sdk_module._global_sdk
    sdk_module._global_sdk = sdk
    try:

        @synaptic.trace
        def my_agent(x):
            return f"got {x}"

        # Even though export() raises, the agent must return normally.
        assert my_agent("hi") == "got hi"

        # And flush() must swallow the exception too.
        sdk.flush()
    finally:
        sdk_module._global_sdk = previous
        sdk.shutdown()


def test_unreachable_server_does_not_break_agent():
    config = Config(
        host="http://127.0.0.1:1",  # port 1 — connection refused fast
        flush_interval=3600.0,
        export_timeout=0.5,
    )
    sdk = SynapticSDK(config=config)
    previous = sdk_module._global_sdk
    sdk_module._global_sdk = sdk
    try:

        @synaptic.trace
        def my_agent(x):
            return x * 2

        assert my_agent(5) == 10
        # flush() actually attempts the HTTP call — must not raise.
        sdk.flush()
    finally:
        sdk_module._global_sdk = previous
        sdk.shutdown()


async def test_unreachable_server_does_not_break_async_agent():
    config = Config(
        host="http://127.0.0.1:1",
        flush_interval=3600.0,
        export_timeout=0.5,
    )
    sdk = SynapticSDK(config=config)
    previous = sdk_module._global_sdk
    sdk_module._global_sdk = sdk
    try:

        @synaptic.trace
        async def async_agent(x):
            await asyncio.sleep(0)
            return x + 1

        assert await async_agent(41) == 42
        sdk.flush()
    finally:
        sdk_module._global_sdk = previous
        sdk.shutdown()


def test_exception_in_traced_function_still_propagates_to_caller(sdk):
    """Errors in user code must propagate; only Synaptic errors are silenced."""

    @synaptic.trace
    def boom():
        raise ValueError("user error")

    with pytest.raises(ValueError):
        boom()


async def test_http_exporter_swallows_connection_error():
    """Direct test: HTTPExporter.export never raises on connection failure."""
    exporter = HTTPExporter(host="http://127.0.0.1:1", timeout=0.5)
    span = Span.create("test")
    span.end()
    # Must not raise.
    await exporter.export([span])
    await exporter.close()
