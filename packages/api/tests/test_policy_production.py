"""Production-readiness regressions for the Policy Engine.

Each test pins one of the five blockers we fixed when promoting the
engine from prototype to production-ready:

  1. Late-arriving children → trace_end re-evaluates on every span
  2. Cleanup cascades to policy_violations
  3. Idempotent inserts: re-ingest cannot create duplicate violations
  4. Eval runs as a background task, off the request path
  5. SSRF guard blocks webhook URLs pointing at private/metadata IPs
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db import Database


@pytest.fixture
def policy_file(tmp_path: Path):
    f = tmp_path / "policies.yaml"
    f.write_text("""
version: 1
policies:
  - name: tool_runaway_loop
    trigger: trace_end
    condition: "trace.span_count > 5"
    action: alert
    severity: high
  - name: any_error
    trigger: trace_end
    condition: "trace.error_count > 0"
    action: flag
    severity: low
  - name: slow_llm
    trigger: span_end
    condition: "span.type == 'llm' and span.duration_ms > 100"
    action: alert
    severity: medium
""", encoding="utf-8")
    import policy_runtime as pr
    old = os.environ.get("LUMIN_POLICY_FILE")
    os.environ["LUMIN_POLICY_FILE"] = str(f)
    pr._engine = None
    pr._engine_loaded = False
    yield f
    pr._engine = None
    pr._engine_loaded = False
    if old is None:
        os.environ.pop("LUMIN_POLICY_FILE", None)
    else:
        os.environ["LUMIN_POLICY_FILE"] = old


def _post(client, **kw):
    return client.post(
        "/v1/spans",
        json={"spans": [{
            "id": kw["id"],
            "trace_id": kw.get("trace_id", kw["id"]),
            "parent_span_id": kw.get("parent_span_id"),
            "name": kw.get("name", "x"),
            "type": kw.get("type", "custom"),
            "started_at": kw.get("started_at", "2026-05-04T10:00:00Z"),
            "ended_at": kw.get("ended_at", "2026-05-04T10:00:00.050Z"),
            "model": kw.get("model"),
            "error": kw.get("error"),
            "status": kw.get("status"),
        }]},
    )


# ---------------------------------------------------------------------------
# Blocker #1 — late-arriving children re-trigger trace_end eval
# ---------------------------------------------------------------------------


def test_late_child_flips_trace_end_policy_on(client, policy_file):
    """Real-world OTel BatchSpanProcessor pattern: root span flushes
    before the last few children. With the v1 'eval at root-end only'
    behavior, tool_runaway_loop never fired because span_count was
    measured at root-end time. With the production fix, every child
    re-evaluates and the policy fires when threshold is crossed."""
    trace_id = "t-late"

    # Root arrives FIRST (this is the BatchSpanProcessor anti-pattern)
    _post(client, id="root-late", trace_id=trace_id, name="agent.run")

    # Then children dribble in afterwards (5 children = 6 total → over the
    # threshold of 5).
    for i in range(5):
        _post(client, id=f"late-child-{i}", trace_id=trace_id, parent_span_id="root-late")

    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "tool_runaway_loop" in names, (
        f"late-child trace_end re-eval failed; got policies: {names}"
    )


def test_root_only_below_threshold_does_not_fire(client, policy_file):
    """Negative — confirm the fix doesn't fire spuriously."""
    _post(client, id="solo-root", trace_id="t-solo", name="lonely")
    listing = client.get("/v1/violations?trace_id=t-solo").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "tool_runaway_loop" not in names


# ---------------------------------------------------------------------------
# Blocker #2 — retention cleanup cascades to policy_violations
# ---------------------------------------------------------------------------


def test_cleanup_sweeps_orphan_violations(db: Database, policy_file):
    """A violation whose trace_id never had a matching trace row
    (i.e. someone POSTed to /v1/violations directly without ever
    ingesting spans for that trace) is an 'orphan from birth'. The
    trace-deletion cascade can't see it because there's no parent
    trace to drive the cascade. Without a separate sweep, these
    accumulate indefinitely — surfaced by the brutal-test SSRF
    gauntlet which posts violation rows for fake trace_ids.
    """
    # Insert an orphan violation old enough to be eligible for cleanup
    cutoff_old = datetime.now(timezone.utc) - timedelta(days=120)
    db.execute(
        """
        INSERT INTO policy_violations (id, policy_name, trace_id, severity,
                                       action_taken, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ["v-orphan-old", "p", "trace-that-never-existed", "low", "flag",
         cutoff_old.replace(tzinfo=None)],
    )
    # And a recent orphan violation that should NOT be cleaned (within retention)
    db.execute(
        """
        INSERT INTO policy_violations (id, policy_name, trace_id, severity,
                                       action_taken, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ["v-orphan-fresh", "p", "another-fake-trace", "low", "flag",
         datetime.now(timezone.utc).replace(tzinfo=None)],
    )
    db.cleanup_old_traces(retention_days=90)
    remaining = {r[0] for r in db.fetchall("SELECT id FROM policy_violations")}
    assert "v-orphan-old" not in remaining, "old orphan violation should be swept"
    assert "v-orphan-fresh" in remaining, "fresh orphan should survive (race window safety)"


def test_cleanup_old_traces_cascades_to_violations(db: Database, policy_file):
    """The retention task deletes old traces; without a cascade,
    policy_violations rows for those traces stayed in the table
    forever. Verify the cascade now hits them."""
    cutoff_old = datetime.now(timezone.utc) - timedelta(days=120)
    cutoff_old_naive = cutoff_old.replace(tzinfo=None)

    # Insert an old trace + a violation pointing at it
    db.execute(
        "INSERT INTO traces (id, name, started_at) VALUES (?, ?, ?)",
        ["old-trace", "old", cutoff_old_naive],
    )
    db.execute(
        """
        INSERT INTO policy_violations (
            id, policy_name, trace_id, severity, action_taken
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ["v-old", "any_policy", "old-trace", "high", "alert"],
    )
    # And a recent trace + violation that should survive
    db.execute(
        "INSERT INTO traces (id, name, started_at) VALUES (?, ?, ?)",
        ["new-trace", "new", datetime.now(timezone.utc).replace(tzinfo=None)],
    )
    db.execute(
        """
        INSERT INTO policy_violations (
            id, policy_name, trace_id, severity, action_taken
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ["v-new", "any_policy", "new-trace", "high", "alert"],
    )

    deleted = db.cleanup_old_traces(retention_days=90)
    assert deleted == 1

    remaining = db.fetchall("SELECT id FROM policy_violations ORDER BY id")
    remaining_ids = {r[0] for r in remaining}
    assert "v-old" not in remaining_ids, "old violation should have been cleaned up"
    assert "v-new" in remaining_ids, "recent violation should survive"


# ---------------------------------------------------------------------------
# Blocker #3 — idempotent inserts: re-ingest cannot duplicate violations
# ---------------------------------------------------------------------------


def test_re_ingesting_same_span_does_not_duplicate_violations(client, policy_file):
    """OTel SDKs sometimes retry the same batch (transient network).
    Without idempotency, every retry creates duplicate violation rows."""
    args = dict(
        id="re-ingest",
        trace_id="t-re",
        type="llm",
        model="gpt-4o",
        started_at="2026-05-04T10:00:00.000Z",
        ended_at="2026-05-04T10:00:00.500Z",  # 500 ms — over 100 ms threshold
    )
    _post(client, **args)
    _post(client, **args)
    _post(client, **args)

    listing = client.get("/v1/violations?trace_id=t-re").json()
    slow = [v for v in listing["violations"] if v["policy_name"] == "slow_llm"]
    assert len(slow) == 1, (
        f"re-ingest dup-fired the policy; expected 1, got {len(slow)}"
    )


def test_repeated_trace_end_evals_dedupe(client, policy_file):
    """Every span ingest now re-evaluates trace_end. With 8 spans,
    the runaway policy is evaluated 8 times — but should land in
    the table exactly once."""
    trace_id = "t-dedup"
    for i in range(7):
        _post(client, id=f"c-{i}", trace_id=trace_id, parent_span_id="root-d")
    _post(client, id="root-d", trace_id=trace_id, name="agent.run")

    listing = client.get(f"/v1/violations?trace_id={trace_id}").json()
    runaway = [v for v in listing["violations"] if v["policy_name"] == "tool_runaway_loop"]
    assert len(runaway) == 1, (
        f"trace_end re-eval should dedupe; got {len(runaway)}"
    )


def test_violations_post_endpoint_also_dedupes(client, policy_file):
    """The /v1/violations POST endpoint (used by the Python SDK
    dispatcher) shares the same idempotency story — repeat POSTs
    of the same violation collapse onto one row. Critical for
    SDK-side + server-side both being active without dup-firing."""
    body = {
        "violations": [{
            "policy_name": "p1",
            "severity": "high",
            "trace_id": "t-post",
            "span_id": "s-post",
            "action_taken": "flag",
        }]
    }
    client.post("/v1/violations", json=body)
    client.post("/v1/violations", json=body)
    client.post("/v1/violations", json=body)
    listing = client.get("/v1/violations?trace_id=t-post").json()
    assert listing["total"] == 1


# ---------------------------------------------------------------------------
# Blocker #4 — eval runs as a background task, off the request path
# ---------------------------------------------------------------------------


def test_ingest_returns_before_eval_completes(client, policy_file):
    """The HTTP response returns immediately; eval runs after.
    We can't directly assert "this returned in <1ms" in a unit test
    portably, but we can confirm:
      - the response shape returns the right `accepted` count
      - the violation lands eventually (background task ran)
    """
    # Post a span that will fire slow_llm
    resp = client.post("/v1/spans", json={"spans": [{
        "id": "bg-1",
        "trace_id": "t-bg",
        "type": "llm",
        "started_at": "2026-05-04T10:00:00.000Z",
        "ended_at": "2026-05-04T10:00:00.500Z",
    }]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}

    # FastAPI's TestClient runs background tasks before returning
    # control, so by the time we get here the eval has run. (In
    # production the response goes out first, then eval runs in
    # the threadpool.) Either way, the violation is now visible.
    listing = client.get("/v1/violations?trace_id=t-bg").json()
    names = {v["policy_name"] for v in listing["violations"]}
    assert "slow_llm" in names


# ---------------------------------------------------------------------------
# Blocker #9 — SSRF guard blocks webhook URLs at metadata / private IPs
# ---------------------------------------------------------------------------


def test_ssrf_guard_strips_metadata_url(client):
    """An admin-controlled YAML can't trick the engine into POSTing
    to AWS instance metadata, GCP/Azure metadata, or any RFC1918 /
    loopback / link-local address."""
    body = {
        "violations": [{
            "policy_name": "p_ssrf_test",
            "severity": "low",
            "trace_id": "t-ssrf",
            "action_taken": "alert",
            # AWS instance metadata service — must be rejected
            "webhook_url": "http://169.254.169.254/latest/meta-data/iam/",
        }]
    }
    client.post("/v1/violations", json=body)
    listing = client.get("/v1/violations?trace_id=t-ssrf").json()
    assert listing["total"] == 1
    # URL was stored as NULL, not the dangerous one
    assert listing["violations"][0]["webhook_url"] is None


def test_ssrf_guard_strips_loopback(client):
    body = {
        "violations": [{
            "policy_name": "p_loopback",
            "severity": "low",
            "trace_id": "t-loop",
            "action_taken": "alert",
            "webhook_url": "http://127.0.0.1:9000/internal-rce",
        }]
    }
    client.post("/v1/violations", json=body)
    listing = client.get("/v1/violations?trace_id=t-loop").json()
    assert listing["violations"][0]["webhook_url"] is None


def test_ssrf_guard_allows_normal_https_url(client):
    body = {
        "violations": [{
            "policy_name": "p_normal",
            "severity": "low",
            "trace_id": "t-ok",
            "action_taken": "alert",
            "webhook_url": "https://hooks.slack.com/services/T/B/X",
        }]
    }
    client.post("/v1/violations", json=body)
    listing = client.get("/v1/violations?trace_id=t-ok").json()
    assert listing["violations"][0]["webhook_url"] == "https://hooks.slack.com/services/T/B/X"


def test_ssrf_guard_allows_dns_name(client):
    """Hostnames (DNS names) are allowed — we don't resolve at
    policy-load time because the resolver itself can be DNS-rebinding'd.
    Operators are expected to vet the YAML."""
    body = {
        "violations": [{
            "policy_name": "p_dns",
            "severity": "low",
            "trace_id": "t-dns",
            "action_taken": "alert",
            "webhook_url": "https://hooks.example.com/path",
        }]
    }
    client.post("/v1/violations", json=body)
    listing = client.get("/v1/violations?trace_id=t-dns").json()
    assert listing["violations"][0]["webhook_url"] == "https://hooks.example.com/path"


def test_ssrf_guard_rejects_unsupported_scheme(client):
    body = {
        "violations": [{
            "policy_name": "p_file",
            "severity": "low",
            "trace_id": "t-file",
            "action_taken": "alert",
            "webhook_url": "file:///etc/passwd",
        }]
    }
    client.post("/v1/violations", json=body)
    listing = client.get("/v1/violations?trace_id=t-file").json()
    assert listing["violations"][0]["webhook_url"] is None


@pytest.mark.parametrize("label,url", [
    ("short-form 127.1",      "http://127.1/x"),
    ("decimal-encoded AWS",   "http://2852039166/iam"),     # = 169.254.169.254
    ("decimal-encoded LB",    "http://2130706433/x"),       # = 127.0.0.1
    ("hex-encoded LB",        "http://0x7f000001/x"),       # = 127.0.0.1
    ("octal-encoded LB",      "http://017700000001/x"),     # = 127.0.0.1
])
def test_ssrf_guard_blocks_creative_ip_encodings(client, label, url):
    """Brutal-test regression: real attackers don't write 127.0.0.1.
    They write 127.1, 2130706433, 0x7f000001 — all of which decode to
    loopback. socket.inet_aton catches every POSIX form."""
    tid = f"t-ssrf-{label.replace(' ', '-')[:24]}"
    body = {
        "violations": [{
            "policy_name": f"ssrf-creative-{label}",
            "severity": "low",
            "trace_id": tid,
            "action_taken": "alert",
            "webhook_url": url,
        }]
    }
    client.post("/v1/violations", json=body)
    listing = client.get(f"/v1/violations?trace_id={tid}").json()
    assert listing["violations"][0]["webhook_url"] is None, (
        f"creative encoding {label!r} bypassed the SSRF guard: {url}"
    )
