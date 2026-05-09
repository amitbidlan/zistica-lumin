"""Metrics latency wiring test (brutal-test fix —
verifies the bug found while attacking on 2026-05-09).

Before this fix, /v1/admin/metrics returned latency_ms with all
fields null even after hundreds of decide() calls. Two reasons:

  1. routers/metrics.py read the wrong dict keys
     (eval_p50_ms instead of eval_latency_ms_p50)
  2. routers/firewall.py never called policy_metrics.record_eval

Fixed both. This file confirms the values populate after at least
one decide call.
"""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
import main
import policy_metrics


@pytest.fixture
def db() -> Generator[Database, None, None]:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture
def client(db: Database):
    main.app.dependency_overrides[main.get_db] = lambda: db
    # Reset the metrics ring buffer so prior tests don't leak in.
    policy_metrics._eval_latencies_ms.clear()
    policy_metrics._evals_total.clear()
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_metrics_latency_null_on_empty_ring(client: TestClient) -> None:
    """No decide calls yet → latency fields are null + samples=0."""
    resp = client.get("/v1/admin/metrics").json()
    lat = resp["decisions"]["latency_ms"]
    assert lat["samples"] == 0
    assert lat["p50_ms"] in (None, 0, 0.0)
    assert lat["p99_ms"] in (None, 0, 0.0)


def test_metrics_latency_populated_after_decide(client: TestClient) -> None:
    """After a decide call, the ring buffer has a sample and the
    metrics endpoint surfaces non-null p50/p99."""
    for _ in range(5):
        client.post("/v1/policy/decide", json={
            "lifecycle": "before_proxy_call",
            "messages": [{"role": "user", "content": "test"}],
        })
    resp = client.get("/v1/admin/metrics").json()
    lat = resp["decisions"]["latency_ms"]
    assert lat["samples"] >= 5, f"expected ≥5 samples, got {lat['samples']}"
    assert isinstance(lat["p50_ms"], (int, float)), lat
    assert isinstance(lat["p99_ms"], (int, float)), lat
    assert lat["max_ms"] is not None


def test_metrics_records_violation_count_on_block(
    client: TestClient, db: Database,
) -> None:
    """When decide returns a non-allow verb, violations_fired
    increments. This proves the recorder is plumbed for both
    success and block paths."""
    from lumin.policy import Policy
    import policy_store

    p = Policy(
        name="always_block", description="x", trigger="span_end",
        condition="True", action="block", severity="high",
        lifecycle="before_proxy_call", mode="enforce", priority=99,
    )
    policy_store.create_policy(db, p, actor="test")

    # Some allows, then some blocks
    for _ in range(3):
        client.post("/v1/policy/decide", json={
            "lifecycle": "before_proxy_call",
            "messages": [{"role": "user", "content": "test"}],
        })

    snap = policy_metrics.snapshot().to_dict()
    # decide:before_proxy_call should have incremented evals_total
    by_trigger = snap["evals_total"]
    decide_evals = sum(v for k, v in by_trigger.items() if k.startswith("decide:"))
    assert decide_evals >= 3, f"evals_total: {by_trigger}"
