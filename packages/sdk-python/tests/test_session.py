"""Tests for lumin.session() — multi-turn session grouping."""

import asyncio
import json

import pytest

import lumin
from lumin import Session


# ---------- Session class basics ----------


def test_session_with_explicit_id():
    s = Session(id="user-123-conv-456")
    assert s.id == "user-123-conv-456"
    assert s.name is None


def test_session_with_name_gets_slug_prefixed_id():
    s = Session(name="Booking Flow!")
    assert s.id.startswith("booking-flow-")
    # slug + 8-char hex suffix
    assert len(s.id) == len("booking-flow-") + 8
    assert s.name == "Booking Flow!"


def test_session_with_no_args_gets_uuid():
    s = Session()
    # Looks like a UUID
    assert len(s.id) == 36
    assert s.id.count("-") == 4


def test_explicit_id_wins_over_name():
    s = Session(id="explicit", name="ignored")
    assert s.id == "explicit"


# ---------- Context manager + propagation ----------


def test_get_current_session_returns_active(sdk):
    assert lumin.get_current_session() is None
    with Session(id="active") as s:
        assert lumin.get_current_session() is s
    assert lumin.get_current_session() is None


def test_session_id_propagates_to_traced_function(sdk, captured):
    @lumin.trace
    def my_agent(q):
        return f"answer: {q}"

    with lumin.session(id="user-1-conv-1"):
        my_agent("hello")
        my_agent("again")

    sdk.flush()
    assert len(captured) == 2
    for s in captured:
        assert s.session_id == "user-1-conv-1"


def test_session_id_propagates_to_nested_spans(sdk, captured):
    @lumin.trace
    def child():
        return "x"

    @lumin.trace
    def parent():
        return child()

    with lumin.session(id="nested-test"):
        parent()
    sdk.flush()

    by_name = {s.name: s for s in captured}
    assert by_name["parent"].session_id == "nested-test"
    assert by_name["child"].session_id == "nested-test"
    assert by_name["parent"].trace_id == by_name["child"].trace_id


def test_no_session_outside_context(sdk, captured):
    @lumin.trace
    def standalone():
        return 1

    standalone()
    sdk.flush()
    assert captured[0].session_id is None


def test_session_only_applies_inside_with(sdk, captured):
    @lumin.trace
    def fn():
        return 1

    fn()  # before — no session

    with lumin.session(id="scoped"):
        fn()  # inside — has session

    fn()  # after — no session

    sdk.flush()
    sessions = [s.session_id for s in captured]
    assert sessions == [None, "scoped", None]


def test_explicit_session_id_on_decorator_overrides_context(sdk, captured):
    @lumin.trace(session_id="from-decorator")
    def fn():
        return 1

    with lumin.session(id="from-context"):
        fn()
    sdk.flush()

    assert captured[0].session_id == "from-decorator"


def test_explicit_session_id_works_outside_context(sdk, captured):
    @lumin.trace(session_id="explicit-only")
    def fn():
        return 1

    fn()
    sdk.flush()
    assert captured[0].session_id == "explicit-only"


# ---------- async ----------


async def test_async_session_context_manager(sdk, captured):
    @lumin.trace
    async def async_agent(q):
        await asyncio.sleep(0)
        return q

    async with lumin.session(id="async-session"):
        await async_agent("first")
        await async_agent("second")
    sdk.flush()

    assert all(s.session_id == "async-session" for s in captured)


async def test_concurrent_async_tasks_share_session_via_contextvar(sdk, captured):
    @lumin.trace
    async def task(name):
        await asyncio.sleep(0.005)
        return name

    async with lumin.session(id="concurrent-test"):
        await asyncio.gather(task("a"), task("b"), task("c"))
    sdk.flush()

    assert all(s.session_id == "concurrent-test" for s in captured)


# ---------- nested sessions (inner overrides outer) ----------


def test_nested_session_inner_wins_inside(sdk, captured):
    @lumin.trace
    def fn():
        return 1

    with lumin.session(id="outer"):
        fn()  # outer
        with lumin.session(id="inner"):
            fn()  # inner
        fn()  # outer again
    sdk.flush()

    sessions = [s.session_id for s in captured]
    assert sessions == ["outer", "inner", "outer"]


# ---------- to_dict shape ----------


def test_to_dict_includes_session_id(sdk, captured):
    @lumin.trace
    def fn():
        return 1

    with lumin.session(id="dict-shape"):
        fn()
    sdk.flush()

    d = captured[0].to_dict()
    assert "session_id" in d
    assert d["session_id"] == "dict-shape"


def test_to_dict_session_id_is_none_when_no_session(sdk, captured):
    @lumin.trace
    def fn():
        return 1

    fn()
    sdk.flush()

    d = captured[0].to_dict()
    assert d["session_id"] is None
