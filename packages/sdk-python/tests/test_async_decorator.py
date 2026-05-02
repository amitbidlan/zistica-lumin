import asyncio
import json

import pytest

import synaptic


async def test_async_decorator_captures_span(sdk, captured):
    @synaptic.trace
    async def async_agent(x: str) -> str:
        await asyncio.sleep(0)
        return f"async {x}"

    result = await async_agent("hi")
    sdk.flush()

    assert result == "async hi"
    assert len(captured) == 1
    s = captured[0]
    assert s.name == "async_agent"
    assert json.loads(s.output) == "async hi"
    assert s.started_at is not None
    assert s.ended_at is not None
    assert s.error is None


async def test_async_decorator_records_exception(sdk, captured):
    @synaptic.trace
    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("oops")

    with pytest.raises(RuntimeError):
        await boom()

    sdk.flush()
    assert len(captured) == 1
    assert "RuntimeError" in captured[0].error


async def test_async_decorator_auto_detection_distinct_from_sync(sdk, captured):
    @synaptic.trace
    async def async_fn():
        return "async"

    @synaptic.trace
    def sync_fn():
        return "sync"

    coro = async_fn()
    assert asyncio.iscoroutine(coro)
    assert await coro == "async"

    assert sync_fn() == "sync"

    sdk.flush()
    names = [s.name for s in captured]
    assert "async_fn" in names
    assert "sync_fn" in names
