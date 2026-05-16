"""Tests for the bearer-token middleware (Slice 5B).

Default-off behavior is the most important property — every existing
test in this suite assumes no token. The middleware only kicks in
when ``LUMIN_API_TOKEN`` is set in the environment, so tests that
exercise auth must set it (and clear it afterwards) explicitly via
monkeypatch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from db import Database
import main


@pytest.fixture
def client():
    """Test client wired to an in-memory DB so we don't fight the
    running API for the on-disk lock. The auth tests only care
    about middleware behavior — the underlying handlers can return
    whatever they like as long as it's not 401 / 403."""
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    db.close()


def _clear_token(monkeypatch) -> None:
    monkeypatch.delenv("LUMIN_API_TOKEN", raising=False)


def _set_token(monkeypatch, token: str) -> None:
    monkeypatch.setenv("LUMIN_API_TOKEN", token)


# ----- default-off behavior ------------------------------------------------


def test_health_open_when_token_unset(client: TestClient, monkeypatch) -> None:
    _clear_token(monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_v1_open_when_token_unset(client: TestClient, monkeypatch) -> None:
    """The whole API was reachable without auth before; nothing
    changes when LUMIN_API_TOKEN is unset."""
    _clear_token(monkeypatch)
    resp = client.get("/v1/traces")
    # 200 (or 5xx if the upstream is broken — but never 401/403)
    assert resp.status_code != 401
    assert resp.status_code != 403


# ----- token enabled — public paths still open -----------------------------


def test_health_open_with_token_set(client: TestClient, monkeypatch) -> None:
    """The Docker healthcheck must keep working even when auth is on."""
    _set_token(monkeypatch, "test-token-123")
    resp = client.get("/health")
    assert resp.status_code == 200


def test_openapi_open_with_token_set(client: TestClient, monkeypatch) -> None:
    _set_token(monkeypatch, "test-token-123")
    resp = client.get("/openapi.json")
    assert resp.status_code == 200


def test_docs_open_with_token_set(client: TestClient, monkeypatch) -> None:
    _set_token(monkeypatch, "test-token-123")
    resp = client.get("/docs")
    assert resp.status_code == 200


# ----- token enabled — protected paths -------------------------------------


def test_v1_returns_401_without_header(client: TestClient, monkeypatch) -> None:
    _set_token(monkeypatch, "test-token-123")
    resp = client.get("/v1/traces")
    assert resp.status_code == 401
    assert resp.json()["error"] == "missing_authorization"


def test_v1_returns_403_with_wrong_token(client: TestClient, monkeypatch) -> None:
    _set_token(monkeypatch, "right-token")
    resp = client.get(
        "/v1/traces",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "invalid_token"


def test_v1_passes_with_correct_token(client: TestClient, monkeypatch) -> None:
    _set_token(monkeypatch, "right-token")
    resp = client.get(
        "/v1/traces",
        headers={"Authorization": "Bearer right-token"},
    )
    # 200 (or other non-auth code) — never 401/403
    assert resp.status_code not in (401, 403)


def test_v1_passes_with_query_string_token(client: TestClient, monkeypatch) -> None:
    """WebSocket-friendly fallback — token can ride in the query
    string for clients that can't cleanly attach headers."""
    _set_token(monkeypatch, "right-token")
    resp = client.get("/v1/traces?token=right-token")
    assert resp.status_code not in (401, 403)


def test_query_string_wrong_token_403(client: TestClient, monkeypatch) -> None:
    _set_token(monkeypatch, "right-token")
    resp = client.get("/v1/traces?token=wrong")
    assert resp.status_code == 403


def test_malformed_authorization_header(client: TestClient, monkeypatch) -> None:
    """Header must be 'Bearer <token>'. Other shapes (Basic, JWT,
    bare token) get treated as missing — 401, not 403, since the
    operator most likely just sent the wrong scheme."""
    _set_token(monkeypatch, "right-token")
    resp = client.get(
        "/v1/traces", headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401


# ----- empty / whitespace tokens are treated as no-token -------------------


def test_empty_string_token_is_off(client: TestClient, monkeypatch) -> None:
    """LUMIN_API_TOKEN='' is treated identically to unset — operators
    can't accidentally lock themselves out by typing the env var
    without a value."""
    monkeypatch.setenv("LUMIN_API_TOKEN", "")
    resp = client.get("/v1/traces")
    assert resp.status_code != 401


def test_whitespace_only_token_is_off(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LUMIN_API_TOKEN", "   ")
    resp = client.get("/v1/traces")
    assert resp.status_code != 401


# ----- constant-time compare property --------------------------------------


def test_constant_time_compare_used(client: TestClient, monkeypatch) -> None:
    """We don't try to detect the timing characteristic in tests
    (CI noise dominates), but we verify both same-length-different
    and different-length tokens get the same 403 status — the only
    portable proxy for 'we used hmac.compare_digest, not =='."""
    _set_token(monkeypatch, "abcdef")
    short = client.get(
        "/v1/traces", headers={"Authorization": "Bearer x"},
    )
    same_len = client.get(
        "/v1/traces", headers={"Authorization": "Bearer aaaaaa"},
    )
    longer = client.get(
        "/v1/traces", headers={"Authorization": "Bearer abcdefgh"},
    )
    assert short.status_code == 403
    assert same_len.status_code == 403
    assert longer.status_code == 403


# ----- safe-by-default: remote exposure without a token --------------------
#
# No token + loopback = open (unchanged). No token + a non-loopback peer
# = refuse, so a Lumin accidentally bound to 0.0.0.0 on a VPS doesn't
# serve every trace + the firewall control plane to the internet.


@pytest.fixture
def remote_client():
    """A TestClient whose transport peer is a public IP (not loopback)."""
    db = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app, client=("203.0.113.7", 44441))
    main.app.dependency_overrides.clear()
    db.close()


def test_remote_v1_blocked_when_no_token(remote_client, monkeypatch) -> None:
    _clear_token(monkeypatch)
    monkeypatch.delenv("LUMIN_ALLOW_INSECURE", raising=False)
    resp = remote_client.get("/v1/traces")
    assert resp.status_code == 403
    assert resp.json()["error"] == "remote_access_requires_auth"


def test_remote_health_still_open_when_no_token(remote_client, monkeypatch) -> None:
    """Probes / container healthcheck must survive remote exposure."""
    _clear_token(monkeypatch)
    monkeypatch.delenv("LUMIN_ALLOW_INSECURE", raising=False)
    assert remote_client.get("/health").status_code == 200


def test_remote_allowed_with_insecure_optin(remote_client, monkeypatch) -> None:
    _clear_token(monkeypatch)
    monkeypatch.setenv("LUMIN_ALLOW_INSECURE", "1")
    resp = remote_client.get("/v1/traces")
    assert resp.status_code not in (401, 403)


def test_remote_allowed_with_token(remote_client, monkeypatch) -> None:
    """Setting a token is the recommended fix — remote then works
    with the bearer header, exactly like the localhost case."""
    _set_token(monkeypatch, "right-token")
    blocked = remote_client.get("/v1/traces")
    assert blocked.status_code == 401  # token path, not the remote 403
    ok = remote_client.get(
        "/v1/traces", headers={"Authorization": "Bearer right-token"},
    )
    assert ok.status_code not in (401, 403)


def test_localhost_still_open_when_no_token(client: TestClient, monkeypatch) -> None:
    """Regression guard: the zero-friction localhost story is intact —
    the default TestClient peer is treated as local."""
    _clear_token(monkeypatch)
    monkeypatch.delenv("LUMIN_ALLOW_INSECURE", raising=False)
    resp = client.get("/v1/traces")
    assert resp.status_code not in (401, 403)
