"""Tests for the webhook outbound + notification channels (§9.10,
§9.11 / Slice 4).

Coverage:

  - Schema migration creates firewall_webhooks + firewall_webhook_failures
  - create_webhook validates kind + required config fields
  - list_webhooks masks secret-shaped config keys
  - severity_min filter excludes low-severity decisions
  - Dispatcher fires Slack / Discord / generic / pagerduty payloads
    in the correct shape (no real network — urlopen monkeypatched)
  - On HTTP failure, retries up to 3 times then writes a DLQ row
  - HTTP endpoints (CRUD + failures) work
  - Decide-engine bridge: a block-class decision triggers the
    dispatcher; allow does not
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from db import Database
from firewall import webhooks as fw_webhooks
import main


@pytest.fixture(autouse=True)
def _reset_executor():
    """Each test gets a clean executor — pending dispatches from
    one test must not leak into the next."""
    yield
    fw_webhooks.shutdown_for_tests()


@pytest.fixture
def db() -> Database:
    d = Database(duckdb_path=":memory:", sqlite_path=":memory:")
    yield d
    d.close()


@pytest.fixture
def client(db: Database):
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# ----- create / validate ---------------------------------------------------


def test_create_slack_webhook(db: Database) -> None:
    wh = fw_webhooks.create_webhook(
        db,
        name="ops",
        kind="slack",
        config={"webhook_url": "https://hooks.slack.com/services/ABC/DEF"},
    )
    assert wh.id.startswith("wh_")
    assert wh.kind == "slack"


def test_create_rejects_unknown_kind(db: Database) -> None:
    with pytest.raises(ValueError):
        fw_webhooks.create_webhook(
            db, name="bad", kind="telnet", config={"url": "tcp://x"},
        )


def test_create_rejects_missing_required_config(db: Database) -> None:
    # slack requires webhook_url
    with pytest.raises(ValueError):
        fw_webhooks.create_webhook(db, name="x", kind="slack", config={})
    # generic requires url
    with pytest.raises(ValueError):
        fw_webhooks.create_webhook(db, name="x", kind="generic", config={})
    # email requires to
    with pytest.raises(ValueError):
        fw_webhooks.create_webhook(db, name="x", kind="email", config={})


def test_create_rejects_invalid_severity(db: Database) -> None:
    with pytest.raises(ValueError):
        fw_webhooks.create_webhook(
            db, name="x", kind="slack",
            config={"webhook_url": "https://x"},
            severity_min="MEH",
        )


# ----- list / mask ---------------------------------------------------------


def test_list_masks_secret_config_keys(db: Database) -> None:
    fw_webhooks.create_webhook(
        db, name="generic-with-secret", kind="generic",
        config={"url": "https://x.example", "hmac_secret": "supersecretvalue123"},
    )
    rows = fw_webhooks.list_webhooks(db)
    assert len(rows) == 1
    assert "supersecretvalue123" not in rows[0]["config"]["hmac_secret"]
    assert rows[0]["config"]["url"] == "https://x.example"


def test_delete_webhook(db: Database) -> None:
    wh = fw_webhooks.create_webhook(
        db, name="x", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/services/x"},
    )
    assert fw_webhooks.delete_webhook(db, wh.id) is True
    assert fw_webhooks.delete_webhook(db, wh.id) is False  # idempotent


# ----- severity filter -----------------------------------------------------


def test_severity_filter_blocks_low(db: Database) -> None:
    """A webhook configured for severity_min=high doesn't fire on a
    low-severity decision."""
    fw_webhooks.create_webhook(
        db, name="high-only", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/x"},
        severity_min="high",
    )
    fired: List[str] = []
    with patch.object(fw_webhooks, "_attempt_with_retries", lambda **k: fired.append(k["webhook"].id)):
        fw_webhooks.fire_for_decision(
            db,
            decision_id="dec_x", decision="block", severity="low",
            policy_name="test_policy", project=None, reason="x",
            trace_id=None, mode_at_decision="enforce",
        )
        fw_webhooks.shutdown_for_tests()
    assert fired == [], "low-severity decision must not fire a high-only webhook"


def test_severity_filter_passes_high(db: Database) -> None:
    fw_webhooks.create_webhook(
        db, name="medium-and-up", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/x"},
        severity_min="medium",
    )
    fired: List[str] = []
    with patch.object(fw_webhooks, "_attempt_with_retries", lambda **k: fired.append(k["webhook"].id)):
        fw_webhooks.fire_for_decision(
            db,
            decision_id="dec_x", decision="block", severity="critical",
            policy_name="test_policy", project=None, reason="x",
            trace_id=None, mode_at_decision="enforce",
        )
        fw_webhooks.shutdown_for_tests()
    assert len(fired) == 1


def test_project_filter_excludes_other_projects(db: Database) -> None:
    fw_webhooks.create_webhook(
        db, name="prod-only", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/x"},
        project_filter="prod",
    )
    fired: List[str] = []
    with patch.object(fw_webhooks, "_attempt_with_retries", lambda **k: fired.append(k["webhook"].id)):
        fw_webhooks.fire_for_decision(
            db,
            decision_id="dec_x", decision="block", severity="high",
            policy_name="test_policy", project="staging", reason="x",
            trace_id=None, mode_at_decision="enforce",
        )
        fw_webhooks.shutdown_for_tests()
    assert fired == [], "staging project decision must not fire prod-only webhook"


# ----- payload shapes ------------------------------------------------------


def test_slack_payload_shape() -> None:
    payload = fw_webhooks._build_payload(
        kind="slack", decision_id="dec_a", decision="block",
        severity="critical", policy_name="my_policy",
        project="default", reason="r1", trace_id="t1",
        mode_at_decision="enforce", webhook_name="n",
    )
    assert "blocks" in payload
    assert "my_policy" in json.dumps(payload)


def test_pagerduty_payload_severity_mapping() -> None:
    p = fw_webhooks._build_payload(
        kind="pagerduty", decision_id="dec", decision="block",
        severity="critical", policy_name="x", project=None,
        reason="", trace_id=None, mode_at_decision="enforce",
        webhook_name="n",
    )
    assert p["payload"]["severity"] == "critical"
    p2 = fw_webhooks._build_payload(
        kind="pagerduty", decision_id="dec", decision="block",
        severity="medium", policy_name="x", project=None,
        reason="", trace_id=None, mode_at_decision="enforce",
        webhook_name="n",
    )
    assert p2["payload"]["severity"] == "warning"


def test_discord_payload_shape() -> None:
    p = fw_webhooks._build_payload(
        kind="discord", decision_id="d", decision="block",
        severity="high", policy_name="p", project=None,
        reason="", trace_id="t", mode_at_decision="enforce",
        webhook_name="n",
    )
    assert "embeds" in p
    assert isinstance(p["embeds"], list)


def test_generic_payload_includes_envelope() -> None:
    p = fw_webhooks._build_payload(
        kind="generic", decision_id="d", decision="block",
        severity="high", policy_name="p", project="x",
        reason="r", trace_id="t", mode_at_decision="enforce",
        webhook_name="n",
    )
    assert p["type"] == "lumin.firewall.decision"
    assert p["decision_id"] == "d"
    assert p["project"] == "x"


# ----- HTTP delivery + retry -----------------------------------------------


def test_deliver_http_signs_with_hmac_when_secret_set() -> None:
    """A generic webhook with hmac_secret signs the body with
    SHA-256 in the X-Lumin-Signature header."""
    captured: Dict[str, Any] = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def getcode(self): return 200

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return FakeResp()

    with patch.object(fw_webhooks.urllib_request, "urlopen", fake_urlopen):
        fw_webhooks._deliver_http_json(
            url="https://x.example", payload={"x": 1}, secret="topsecret",
        )

    sig_key = next(
        (k for k in captured["headers"] if k.lower() == "x-lumin-signature"),
        None,
    )
    assert sig_key is not None
    assert captured["headers"][sig_key].startswith("sha256=")


@pytest.fixture
def disk_db(tmp_path):
    """Tempfile-backed DB for tests that exercise the worker-thread
    write paths. The worker opens its own Database connection from
    ``duckdb_path``, which means ``:memory:`` becomes a new empty
    DB on each open — not what the test intends. A real file makes
    cross-connection writes visible."""
    duck = tmp_path / "wh-test.duckdb"
    sqlite = tmp_path / "wh-test.sqlite"
    d = Database(duckdb_path=str(duck), sqlite_path=str(sqlite))
    yield d
    d.close()


def test_deliver_failure_writes_dlq(disk_db: Database) -> None:
    """When all 3 attempts fail, a DLQ row appears."""
    db = disk_db
    wh = fw_webhooks.create_webhook(
        db, name="bad", kind="slack",
        config={"webhook_url": "https://will.fail/never-reachable"},
    )
    payload = {"text": "hi"}

    # Force every delivery attempt to raise. Patch backoff to 0 so
    # the test finishes fast.
    with patch.object(
        fw_webhooks, "_deliver_once",
        side_effect=RuntimeError("simulated"),
    ), patch.object(fw_webhooks, "_BACKOFF_SECONDS", (0, 0, 0)):
        fw_webhooks._attempt_with_retries(
            webhook=fw_webhooks.WebhookConfig(
                id=wh.id, name=wh.name, kind=wh.kind, config=wh.config,
                severity_min=wh.severity_min, project_filter=wh.project_filter,
                active=True,
            ),
            payload=payload,
            decision_id="dec_test",
            db=db,
        )

    failures = fw_webhooks.list_failures(db)
    assert len(failures) == 1
    assert failures[0]["webhook_id"] == wh.id
    assert failures[0]["attempt_count"] == 3
    assert "simulated" in (failures[0]["last_error"] or "")


def test_deliver_success_no_dlq(disk_db: Database) -> None:
    db = disk_db
    wh = fw_webhooks.create_webhook(
        db, name="good", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/x"},
    )
    with patch.object(fw_webhooks, "_deliver_once", return_value=None):
        fw_webhooks._attempt_with_retries(
            webhook=fw_webhooks.WebhookConfig(
                id=wh.id, name=wh.name, kind=wh.kind, config=wh.config,
                severity_min=wh.severity_min, project_filter=wh.project_filter,
                active=True,
            ),
            payload={"x": 1},
            decision_id="dec_a",
            db=db,
        )
    assert fw_webhooks.list_failures(db) == []


def test_successful_delivery_marks_last_fired_at(disk_db: Database) -> None:
    """Regression for issue #114: a successful webhook delivery must
    update ``last_fired_at`` on the webhook row. The previous worker
    opened a fresh ``Database(duckdb_path, sqlite_path)`` from inside
    the executor thread — which silently failed (DuckDB single-writer
    semantics) so ``_mark_fired`` raised, the logger swallowed it,
    and the row stayed at ``last_fired_at=NULL`` forever even though
    the HTTP delivery succeeded."""
    db = disk_db
    wh = fw_webhooks.create_webhook(
        db, name="ok-channel", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/x"},
    )
    # Sanity: brand-new row has no fire timestamp yet.
    before = db.fetchone(
        "SELECT last_fired_at FROM firewall_webhooks WHERE id = ?", [wh.id]
    )
    assert before is not None and before[0] is None

    with patch.object(fw_webhooks, "_deliver_once", return_value=None):
        fw_webhooks._attempt_with_retries(
            webhook=fw_webhooks.WebhookConfig(
                id=wh.id, name=wh.name, kind=wh.kind, config=wh.config,
                severity_min=wh.severity_min, project_filter=wh.project_filter,
                active=True,
            ),
            payload={"hello": "world"},
            decision_id="dec_success",
            db=db,
        )

    after = db.fetchone(
        "SELECT last_fired_at, last_error FROM firewall_webhooks WHERE id = ?",
        [wh.id],
    )
    assert after is not None
    assert after[0] is not None, (
        "last_fired_at must be set after a successful delivery; "
        "if this is None the worker thread's write to firewall_webhooks "
        "is being silently swallowed (issue #114)"
    )
    assert after[1] is None, "last_error must be None on success"
    assert fw_webhooks.list_failures(db) == []


def test_fire_for_decision_dispatches_for_rewrite(disk_db: Database) -> None:
    """Regression for issue #114: a ``rewrite`` decision must dispatch
    a webhook just like ``block``. The README documents webhooks fire
    on ``every block, redaction, or policy hit``; ``decide.py`` (L997)
    confirms the intent (``decision in {block, require_approval,
    rewrite}``). The bug was that the dispatch ran but the worker
    thread couldn't write back, so externally it looked like rewrite
    silently skipped webhooks."""
    db = disk_db
    wh = fw_webhooks.create_webhook(
        db, name="rewrite-listener", kind="slack",
        config={"webhook_url": "https://hooks.slack.com/x"},
        severity_min="low",  # accept anything
    )

    with patch.object(fw_webhooks, "_deliver_once", return_value=None):
        scheduled = fw_webhooks.fire_for_decision(
            db,
            decision_id="dec_rewrite_probe",
            decision="rewrite",
            severity="high",
            policy_name="owasp_llm02_pii_disclosure",
            project=None,
            reason="probe",
            trace_id="00000000-0000-0000-0000-0000000000aa",
            mode_at_decision="enforce",
        )
        fw_webhooks.shutdown_for_tests()

    assert scheduled == 1, "rewrite decision must schedule the webhook"
    after = db.fetchone(
        "SELECT last_fired_at FROM firewall_webhooks WHERE id = ?", [wh.id]
    )
    assert after is not None and after[0] is not None, (
        "last_fired_at must advance after a rewrite dispatch (#114)"
    )


# ----- HTTP surface --------------------------------------------------------


def test_create_and_list_via_api(client: TestClient) -> None:
    resp = client.post(
        "/v1/firewall/webhooks",
        json={
            "name": "ops",
            "kind": "slack",
            "config": {"webhook_url": "https://hooks.slack.com/x"},
            "severity_min": "high",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"].startswith("wh_")
    assert body["kind"] == "slack"

    listing = client.get("/v1/firewall/webhooks").json()
    assert len(listing["webhooks"]) == 1


def test_create_invalid_kind_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/firewall/webhooks",
        json={"name": "x", "kind": "carrier-pigeon", "config": {}},
    )
    assert resp.status_code == 400


def test_delete_via_api(client: TestClient) -> None:
    create = client.post(
        "/v1/firewall/webhooks",
        json={"name": "x", "kind": "slack",
              "config": {"webhook_url": "https://hooks.slack.com/x"}},
    ).json()
    wh_id = create["id"]
    assert client.delete(f"/v1/firewall/webhooks/{wh_id}").status_code == 200
    assert client.delete(f"/v1/firewall/webhooks/{wh_id}").status_code == 404


def test_failures_endpoint_returns_empty_at_start(client: TestClient) -> None:
    resp = client.get("/v1/firewall/webhooks/failures")
    assert resp.status_code == 200
    assert resp.json() == {"failures": []}
