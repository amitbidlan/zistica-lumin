"""SSRF guard tests for webhook config (brutal-test fix —
verifies the security hole found on 2026-05-09).

Before this fix, an operator (or anyone with write access to
/v1/firewall/webhooks) could create a webhook pointed at:
  - http://169.254.169.254/latest/meta-data/  (AWS instance creds)
  - http://127.0.0.1:8000/v1/admin/backups    (local admin)
  - file:///etc/passwd                        (filesystem)
  - gopher:// / ftp:// / etc.                 (other schemes)

The legacy ``policy_runtime._webhook_url_safe`` already had the
right logic; it just wasn't being applied to the new
``firewall_webhooks`` table.

This test file confirms each of those attack vectors is now
rejected at create time.
"""

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


def _create(client: TestClient, *, kind: str, url: str) -> int:
    key = "url" if kind == "generic" else "webhook_url"
    resp = client.post(
        "/v1/firewall/webhooks",
        json={"name": "ssrf-test", "kind": kind, "config": {key: url}},
    )
    return resp.status_code


# ----- positive controls — legitimate URLs accepted ----------------------


def test_https_external_url_accepted(client: TestClient) -> None:
    assert _create(client, kind="generic", url="https://hooks.example.com/abc") == 200


def test_slack_webhook_url_accepted(client: TestClient) -> None:
    assert _create(
        client, kind="slack",
        url="https://hooks.slack.com/services/T0/B0/abcdefghij",
    ) == 200


# ----- the four vectors found while attacking ---------------------------


def test_aws_metadata_host_rejected(client: TestClient) -> None:
    """The biggest concern — exfiltrating cloud creds."""
    code = _create(client, kind="generic", url="http://169.254.169.254/latest/meta-data/")
    assert code == 400


def test_aws_metadata_alternate_encoding_rejected(client: TestClient) -> None:
    """169.254.169.254 in alternate forms (decimal, octal) — the
    underlying _webhook_url_safe normalises to IPv4Address."""
    # Decimal int form: 169*256^3 + 254*256^2 + 169*256 + 254 = 2852039166
    code = _create(client, kind="generic", url="http://2852039166/latest/meta-data/")
    assert code == 400


def test_loopback_127_0_0_1_rejected(client: TestClient) -> None:
    """Stops the localhost-admin pivot."""
    code = _create(client, kind="generic", url="http://127.0.0.1:8000/v1/admin/backups")
    assert code == 400


def test_loopback_localhost_rejected(client: TestClient) -> None:
    code = _create(client, kind="generic", url="http://localhost:8000/admin")
    assert code == 400


def test_file_scheme_rejected(client: TestClient) -> None:
    """file:// is silly but Python's urllib will happily attempt it."""
    code = _create(client, kind="generic", url="file:///etc/passwd")
    assert code == 400


def test_gopher_scheme_rejected(client: TestClient) -> None:
    """gopher://, ftp://, ldap:// — anything that isn't http(s)."""
    code = _create(client, kind="generic", url="gopher://attacker.example/exfil")
    assert code == 400


def test_private_ip_rejected(client: TestClient) -> None:
    """RFC1918 — internal services should never be webhook destinations."""
    for url in (
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
    ):
        assert _create(client, kind="generic", url=url) == 400, f"should reject {url}"


def test_link_local_rejected(client: TestClient) -> None:
    """169.254.0.0/16 covers AWS metadata + IPv4 link-local."""
    assert _create(client, kind="generic", url="http://169.254.1.1/") == 400


def test_slack_kind_url_also_validated(client: TestClient) -> None:
    """Slack-kind webhooks check ``webhook_url`` config key — the
    SSRF guard applies to that field too, not just generic ``url``."""
    code = _create(
        client, kind="slack",
        url="http://169.254.169.254/exfil",
    )
    assert code == 400


# ----- absent / empty URL doesn't bypass via no-op ----------------------


def test_missing_required_url_still_400(client: TestClient) -> None:
    """The original required-field validator still works — ensures
    the SSRF guard doesn't silently swallow validation errors."""
    resp = client.post(
        "/v1/firewall/webhooks",
        json={"name": "x", "kind": "slack", "config": {}},
    )
    assert resp.status_code == 400


# ----- DLQ failure log doesn't accidentally store a rejected URL -------


def test_rejected_webhook_does_not_persist(client: TestClient) -> None:
    """Defense in depth — even if the SSRF check fails closed, no
    row should land in firewall_webhooks for an attempted-and-rejected
    creation."""
    _create(client, kind="generic", url="http://169.254.169.254/")
    listing = client.get("/v1/firewall/webhooks").json()
    assert listing["webhooks"] == []
