import json

import synaptic


def test_sync_decorator_captures_span(sdk, captured):
    @synaptic.trace
    def my_agent(x: str) -> str:
        return f"hello {x}"

    result = my_agent("world")
    sdk.flush()

    assert result == "hello world"
    assert len(captured) == 1
    s = captured[0]
    assert s.name == "my_agent"
    assert s.type == "custom"
    assert s.started_at is not None
    assert s.ended_at is not None
    assert s.error is None
    assert s.parent_span_id is None
    assert s.trace_id == s.id  # root span: trace_id == id


def test_sync_decorator_serializes_input_and_output(sdk, captured):
    @synaptic.trace
    def add(a: int, b: int) -> int:
        return a + b

    add(2, 3)
    sdk.flush()

    s = captured[0]
    parsed_input = json.loads(s.input)
    assert parsed_input == {"args": [2, 3], "kwargs": {}}
    assert json.loads(s.output) == 5


def test_sync_decorator_records_exception(sdk, captured):
    @synaptic.trace
    def boom():
        raise ValueError("bad")

    try:
        boom()
    except ValueError:
        pass

    sdk.flush()
    assert len(captured) == 1
    s = captured[0]
    assert "ValueError" in s.error
    assert "bad" in s.error
    assert s.ended_at is not None


def test_decorator_with_explicit_name_and_type(sdk, captured):
    @synaptic.trace(name="custom_step", type="tool")
    def step():
        return "ok"

    step()
    sdk.flush()

    assert captured[0].name == "custom_step"
    assert captured[0].type == "tool"


def test_to_dict_has_required_fields(sdk, captured):
    @synaptic.trace
    def f():
        return 1

    f()
    sdk.flush()

    d = captured[0].to_dict()
    for key in (
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
        assert key in d
