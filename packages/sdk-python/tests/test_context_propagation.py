import asyncio

import lumin
from lumin.context import get_current_span


def test_nested_sync_spans_have_correct_parent(sdk, captured):
    @lumin.trace
    def child():
        return "child"

    @lumin.trace
    def parent():
        return child()

    parent()
    sdk.flush()

    by_name = {s.name: s for s in captured}
    p = by_name["parent"]
    c = by_name["child"]

    assert p.parent_span_id is None
    assert p.trace_id == p.id

    assert c.parent_span_id == p.id
    assert c.trace_id == p.trace_id


def test_three_levels_of_nesting(sdk, captured):
    @lumin.trace
    def level3():
        return 3

    @lumin.trace
    def level2():
        return level3()

    @lumin.trace
    def level1():
        return level2()

    level1()
    sdk.flush()

    by_name = {s.name: s for s in captured}
    l1 = by_name["level1"]
    l2 = by_name["level2"]
    l3 = by_name["level3"]

    assert l1.parent_span_id is None
    assert l2.parent_span_id == l1.id
    assert l3.parent_span_id == l2.id

    assert l1.trace_id == l2.trace_id == l3.trace_id


def test_span_context_manager_nests_under_decorator(sdk, captured):
    @lumin.trace
    def parent():
        with lumin.span("inner_block", type="retrieval"):
            pass

    parent()
    sdk.flush()

    by_name = {s.name: s for s in captured}
    p = by_name["parent"]
    inner = by_name["inner_block"]

    assert inner.parent_span_id == p.id
    assert inner.trace_id == p.trace_id
    assert inner.type == "retrieval"


def test_get_current_span_returns_active_span(sdk):
    @lumin.trace
    def fn():
        current = get_current_span()
        assert current is not None
        assert current.name == "fn"

    fn()


def test_after_function_exits_no_current_span(sdk):
    @lumin.trace
    def fn():
        return 1

    fn()
    assert get_current_span() is None


async def test_concurrent_async_tasks_have_isolated_contexts(sdk, captured):
    results = {}

    @lumin.trace
    async def task_a():
        await asyncio.sleep(0.01)
        with lumin.span("inner_a"):
            current = get_current_span()
            results["a"] = current.name

    @lumin.trace
    async def task_b():
        await asyncio.sleep(0.01)
        with lumin.span("inner_b"):
            current = get_current_span()
            results["b"] = current.name

    await asyncio.gather(task_a(), task_b())
    sdk.flush()

    assert results == {"a": "inner_a", "b": "inner_b"}

    by_name = {s.name: s for s in captured}
    assert by_name["inner_a"].parent_span_id == by_name["task_a"].id
    assert by_name["inner_b"].parent_span_id == by_name["task_b"].id
    # The two tasks must have separate trace ids (separate roots)
    assert by_name["task_a"].trace_id != by_name["task_b"].trace_id
