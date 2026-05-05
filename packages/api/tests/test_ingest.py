def test_ingest_curl_example_from_session_prompt(client):
    """Exact payload shape from the Session 2 curl example must work."""
    response = client.post(
        "/v1/spans",
        json={
            "spans": [
                {
                    "id": "test-123",
                    "name": "my_agent",
                    "type": "custom",
                    "started_at": "2026-05-02T10:00:00Z",
                    "ended_at": "2026-05-02T10:00:01Z",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 1}


def test_ingest_returns_accepted_count(client):
    spans = [
        {
            "id": f"span-{i}",
            "trace_id": "trace-1",
            "name": f"step-{i}",
            "type": "custom",
            "started_at": "2026-05-02T10:00:00Z",
        }
        for i in range(5)
    ]
    response = client.post("/v1/spans", json={"spans": spans})
    assert response.status_code == 200
    assert response.json() == {"accepted": 5}


def test_ingest_is_idempotent_by_span_id(client):
    span = {
        "id": "fixed-id",
        "trace_id": "trace-1",
        "name": "agent",
        "started_at": "2026-05-02T10:00:00Z",
    }
    client.post("/v1/spans", json={"spans": [span]})
    client.post("/v1/spans", json={"spans": [span]})
    response = client.get("/v1/traces/trace-1/spans")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_ingest_creates_trace_from_root_span(client):
    span = {
        "id": "root-1",
        "trace_id": "root-1",
        "name": "my_agent",
        "type": "custom",
        "input": '"hello"',
        "output": '"world"',
        "started_at": "2026-05-02T10:00:00Z",
        "ended_at": "2026-05-02T10:00:01Z",
    }
    client.post("/v1/spans", json={"spans": [span]})
    response = client.get("/v1/traces/root-1")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "root-1"
    assert body["name"] == "my_agent"
    assert body["input"] == '"hello"'
    assert body["output"] == '"world"'
    assert body["started_at"] is not None
    assert body["ended_at"] is not None


def test_ingest_with_error_field_records_error_status(client):
    span = {
        "id": "err-1",
        "trace_id": "err-1",
        "name": "boom",
        "started_at": "2026-05-02T10:00:00Z",
        "error": "ValueError: bad input",
    }
    client.post("/v1/spans", json={"spans": [span]})
    response = client.get("/v1/traces/err-1/spans")
    assert response.status_code == 200
    spans = response.json()
    assert len(spans) == 1
    assert spans[0]["status"] == "error"
    assert spans[0]["error_message"] == "ValueError: bad input"


def test_ingest_creates_stub_trace_for_orphan_span(client):
    """A non-root span arriving before its root must still create a queryable trace."""
    span = {
        "id": "child-1",
        "trace_id": "trace-99",
        "parent_span_id": "root-99",
        "name": "child",
        "started_at": "2026-05-02T10:00:01Z",
    }
    client.post("/v1/spans", json={"spans": [span]})
    response = client.get("/v1/traces/trace-99")
    assert response.status_code == 200
    assert response.json()["id"] == "trace-99"


def test_utc_timestamp_round_trips(client):
    """Z-suffixed UTC input must round-trip without local-TZ shift."""
    span = {
        "id": "tz-1",
        "trace_id": "tz-1",
        "name": "tz_check",
        "started_at": "2026-05-02T10:00:00Z",
        "ended_at": "2026-05-02T10:00:01Z",
    }
    client.post("/v1/spans", json={"spans": [span]})
    body = client.get("/v1/traces/tz-1").json()
    # Stored as naive UTC — readback hour must equal input hour.
    assert body["started_at"].startswith("2026-05-02T10:00:00")
    assert body["ended_at"].startswith("2026-05-02T10:00:01")


def test_root_span_overwrites_stub_trace(client):
    """Stub created by orphan span must be replaced when root span arrives."""
    child = {
        "id": "child-2",
        "trace_id": "t-2",
        "parent_span_id": "root-2",
        "name": "child",
        "started_at": "2026-05-02T10:00:01Z",
    }
    root = {
        "id": "t-2",
        "trace_id": "t-2",
        "name": "root_agent",
        "started_at": "2026-05-02T10:00:00Z",
        "ended_at": "2026-05-02T10:00:02Z",
    }
    client.post("/v1/spans", json={"spans": [child]})
    client.post("/v1/spans", json={"spans": [root]})
    response = client.get("/v1/traces/t-2")
    assert response.json()["name"] == "root_agent"
    assert response.json()["ended_at"] is not None


# ---- Brutal-test regressions: malformed timestamps must NOT 5xx -----------


def test_malformed_started_at_returns_400(client):
    """started_at that doesn't parse as ISO 8601 must produce a clean
    4xx, not a 500. Pre-fix this hit the NOT NULL DB constraint and
    bubbled up as a 500 internal error — discovered by the brutal
    test suite (case H2).
    """
    response = client.post("/v1/spans", json={"spans": [{
        "id": "bad-ts-1",
        "trace_id": "bad-ts-1",
        "name": "agent",
        "started_at": "yesterday at 5pm",
    }]})
    assert response.status_code == 400
    assert "started_at" in response.json()["detail"]


def test_empty_started_at_returns_400(client):
    """Empty string is also unparseable — same 400 path."""
    response = client.post("/v1/spans", json={"spans": [{
        "id": "bad-ts-2",
        "trace_id": "bad-ts-2",
        "name": "agent",
        "started_at": "",
    }]})
    assert response.status_code == 400


def test_partial_batch_failure_aborts_whole_batch(client):
    """One bad timestamp in a batch must reject the entire batch —
    we don't want partial writes since policy eval depends on
    ordered ingest of the whole trace."""
    response = client.post("/v1/spans", json={"spans": [
        {"id": "ok-1", "trace_id": "ok-1", "name": "a", "started_at": "2026-05-02T10:00:00Z"},
        {"id": "bad-1", "trace_id": "bad-1", "name": "b", "started_at": "garbage"},
    ]})
    assert response.status_code == 400
    # Confirm the good span did NOT land
    r = client.get("/v1/traces/ok-1")
    assert r.status_code == 404


def test_openclaw_root_spans_fold_into_one_agent(client):
    """Three different openclaw.* root-span names ingest as three
    traces, all named 'openclaw' — agent grid sees one card."""
    payload = {"spans": [
        {"id": "t1", "trace_id": "t1", "name": "openclaw.run",
         "started_at": "2026-05-06T05:00:00Z"},
        {"id": "t2", "trace_id": "t2", "name": "openclaw.harness.run",
         "started_at": "2026-05-06T05:00:01Z"},
        {"id": "t3", "trace_id": "t3", "name": "openclaw.llm",
         "started_at": "2026-05-06T05:00:02Z"},
    ]}
    r = client.post("/v1/spans", json=payload)
    assert r.status_code == 200

    for tid in ("t1", "t2", "t3"):
        trace = client.get(f"/v1/traces/{tid}").json()
        assert trace["name"] == "openclaw", f"{tid} got name={trace['name']!r}"

    spans = client.get("/v1/traces/t1/spans").json()
    assert spans[0]["name"] == "openclaw"


def test_child_openclaw_spans_keep_phase_names(client):
    """Children of an openclaw root keep their original phase names so
    the timeline still shows model.call / tool.execution / etc."""
    payload = {"spans": [
        {"id": "root", "trace_id": "root", "name": "openclaw.run",
         "started_at": "2026-05-06T05:00:00Z"},
        {"id": "child", "trace_id": "root", "parent_span_id": "root",
         "name": "openclaw.model.call", "type": "llm",
         "started_at": "2026-05-06T05:00:01Z"},
    ]}
    r = client.post("/v1/spans", json=payload)
    assert r.status_code == 200

    spans = client.get("/v1/traces/root/spans").json()
    by_id = {s["id"]: s for s in spans}
    assert by_id["root"]["name"] == "openclaw"
    assert by_id["child"]["name"] == "openclaw.model.call"


def test_non_openclaw_names_pass_through(client):
    """The fold is namespaced — anything that doesn't start with
    a known multi-phase prefix is left alone."""
    r = client.post("/v1/spans", json={"spans": [
        {"id": "x", "trace_id": "x", "name": "my-custom-agent",
         "started_at": "2026-05-06T05:00:00Z"},
    ]})
    assert r.status_code == 200
    assert client.get("/v1/traces/x").json()["name"] == "my-custom-agent"
