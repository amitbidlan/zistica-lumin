import os

# Disable the background retention task during tests — it sleeps 24h by
# default so it would never fire anyway, but making it explicit avoids
# any surprise if interval is shortened later.
os.environ.setdefault("LUMIN_CLEANUP_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from db import Database, get_db
from main import app


@pytest.fixture
def db() -> Database:
    """Fresh in-memory DuckDB + SQLite per test."""
    database = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield database
    database.close()


@pytest.fixture
def client(db: Database):
    """FastAPI test client with the in-memory DB injected."""

    def _override():
        return db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
