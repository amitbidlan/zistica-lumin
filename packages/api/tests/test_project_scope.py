"""Tests for multi-tenant project filtering on read endpoints
(Slice 6B)."""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from db import Database
import main


@pytest.fixture
def db() -> Generator[Database, None, None]:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture
def client(db: Database):
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _seed_traces(db: Database) -> None:
    """Seed traces across three projects."""
    db.execute(
        "INSERT INTO traces (id, name, started_at, project, session_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ["t-prod-1", "agent_a", "2026-05-01 00:00:00", "prod", "s-prod-1"],
    )
    db.execute(
        "INSERT INTO traces (id, name, started_at, project, session_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ["t-prod-2", "agent_a", "2026-05-02 00:00:00", "prod", "s-prod-1"],
    )
    db.execute(
        "INSERT INTO traces (id, name, started_at, project, session_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ["t-staging-1", "agent_b", "2026-05-03 00:00:00", "staging", "s-staging-1"],
    )
    db.execute(
        "INSERT INTO traces (id, name, started_at, project, session_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ["t-untagged", "agent_c", "2026-05-04 00:00:00", None, "s-untagged"],
    )


# ----- /v1/traces ---------------------------------------------------------


def test_traces_no_filter_returns_all(client: TestClient, db: Database) -> None:
    _seed_traces(db)
    resp = client.get("/v1/traces")
    body = resp.json()
    assert {t["id"] for t in body} == {
        "t-prod-1", "t-prod-2", "t-staging-1", "t-untagged",
    }


def test_traces_filter_by_project(client: TestClient, db: Database) -> None:
    _seed_traces(db)
    resp = client.get("/v1/traces?project=prod")
    ids = {t["id"] for t in resp.json()}
    assert ids == {"t-prod-1", "t-prod-2"}


def test_traces_filter_default_includes_untagged(
    client: TestClient, db: Database,
) -> None:
    """project=default should match both NULL and the literal 'default'
    so traces ingested without an X-Lumin-Project header still
    surface in the operator's default bucket."""
    _seed_traces(db)
    db.execute(
        "INSERT INTO traces (id, name, started_at, project) "
        "VALUES ('t-default', 'agent_d', '2026-05-05 00:00:00', 'default')",
    )
    resp = client.get("/v1/traces?project=default")
    ids = {t["id"] for t in resp.json()}
    assert ids == {"t-untagged", "t-default"}


def test_traces_unknown_project_returns_empty(
    client: TestClient, db: Database,
) -> None:
    _seed_traces(db)
    resp = client.get("/v1/traces?project=does-not-exist")
    assert resp.json() == []


# ----- /v1/sessions -------------------------------------------------------


def test_sessions_filter_by_project(client: TestClient, db: Database) -> None:
    _seed_traces(db)
    resp = client.get("/v1/sessions?project=prod")
    ids = {s["session_id"] for s in resp.json()}
    assert ids == {"s-prod-1"}


def test_sessions_no_filter_returns_all(client: TestClient, db: Database) -> None:
    _seed_traces(db)
    resp = client.get("/v1/sessions")
    ids = {s["session_id"] for s in resp.json()}
    assert ids == {"s-prod-1", "s-staging-1", "s-untagged"}


# ----- /v1/violations -----------------------------------------------------


def _seed_violations(db: Database) -> None:
    """Seed violations linked to the seeded traces."""
    _seed_traces(db)
    for vid, tid in (
        ("v-prod-1", "t-prod-1"),
        ("v-prod-2", "t-prod-2"),
        ("v-staging-1", "t-staging-1"),
    ):
        db.execute(
            """
            INSERT INTO policy_violations (
                id, policy_name, trace_id, condition_text, action_taken,
                severity
            ) VALUES (?, 'p', ?, 'c', 'block', 'high')
            """,
            [vid, tid],
        )


def test_violations_filter_by_project(client: TestClient, db: Database) -> None:
    _seed_violations(db)
    resp = client.get("/v1/violations?project=prod")
    ids = {v["id"] for v in resp.json()["violations"]}
    assert ids == {"v-prod-1", "v-prod-2"}


def test_violations_no_filter_returns_all(client: TestClient, db: Database) -> None:
    _seed_violations(db)
    resp = client.get("/v1/violations")
    ids = {v["id"] for v in resp.json()["violations"]}
    assert ids == {"v-prod-1", "v-prod-2", "v-staging-1"}


def test_violations_filter_combines_with_severity(
    client: TestClient, db: Database,
) -> None:
    _seed_violations(db)
    db.execute(
        """
        INSERT INTO policy_violations (
            id, policy_name, trace_id, condition_text, action_taken,
            severity
        ) VALUES ('v-prod-low', 'p', 't-prod-1', 'c', 'flag', 'low')
        """,
    )
    resp = client.get("/v1/violations?project=prod&severity=high")
    ids = {v["id"] for v in resp.json()["violations"]}
    assert ids == {"v-prod-1", "v-prod-2"}
