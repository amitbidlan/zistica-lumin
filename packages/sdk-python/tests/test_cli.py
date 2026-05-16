"""Tests for the `lumin` CLI.

No live server and no HTTP mocking lib: we monkeypatch the CLI's three
HTTP seam functions (`_api_reachable`, `_post`, `_get`) so the real
command logic — scoring math, quiet/JSON mode, block counting, graceful
failure, panel-width math — runs deterministically offline.
"""

from __future__ import annotations

import json

import pytest

from lumin import cli


# ---------- pure helpers ----------------------------------------------------


def test_vis_strips_ansi():
    coloured = "\033[31mred\033[0m"
    assert cli._vis(coloured) == 3
    assert cli._vis("plain") == 5


@pytest.mark.parametrize(
    "pct,grade",
    [(95, "A"), (90, "A"), (80, "B"), (75, "B"), (60, "C"),
     (40, "D"), (39.9, "F"), (0, "F")],
)
def test_grade_boundaries(pct, grade):
    assert cli._grade(pct) == grade


def test_bar_width_is_stable():
    # Visible width must be constant regardless of percentage so panels align.
    widths = {cli._vis(cli._bar(p, width=20)) for p in (0, 1, 50, 99, 100)}
    assert widths == {20}


def test_parser_wires_subcommands():
    p = cli._build_parser()
    for sub in ("up", "demo", "doctor", "scorecard"):
        ns = p.parse_args([sub])
        assert callable(ns.func)


def test_version(capsys):
    rc = cli.main(["--version"])
    assert rc == 0
    assert "lumin" in capsys.readouterr().out


def test_no_command_shows_branded_splash(capsys):
    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "LUMIN" in out                       # branded header, not argparse
    for cmd in ("lumin up", "lumin demo", "lumin scorecard", "lumin doctor"):
        assert cmd in out
    assert "usage:" not in out                  # splash, not raw argparse


def test_dash_h_still_shows_argparse_usage(capsys):
    with pytest.raises(SystemExit) as e:        # argparse exits on -h
        cli.main(["-h"])
    assert e.value.code == 0
    assert "usage:" in capsys.readouterr().out


# ---------- graceful failure (no server) ------------------------------------


@pytest.mark.parametrize("command", ["doctor", "demo", "scorecard"])
def test_commands_fail_gracefully_without_server(command, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_api_reachable", lambda host: False)
    argv = [command, "--host", "http://127.0.0.1:9"]
    if command == "demo":
        argv.append("--no-open")
    rc = cli.main(argv)
    assert rc == 1                                   # non-zero, but no traceback
    assert "lumin up" in capsys.readouterr().out     # actionable next step shown


# ---------- scorecard scoring + JSON purity ---------------------------------


def _wire_fake_lumin(monkeypatch, *, attacks, block_pred, decisions):
    """Install fake HTTP seams. `block_pred(req)->bool` decides enforce
    blocks; `decisions` is the rows /v1/decisions returns."""
    seen_trace_ids = []
    monkeypatch.setattr(cli, "_api_reachable", lambda host: True)
    monkeypatch.setattr(cli, "_client", lambda host: object())

    def fake_post(client, path, body):
        if path.endswith("/generate_attacks"):
            return 200, {"attacks": attacks, "count": len(attacks)}
        if path.endswith("/policy/decide"):
            seen_trace_ids.append(body.get("trace_id"))
            return 200, ({"decision": "block", "policy_name": "p", "duration_ms": 3}
                         if block_pred(body) else {"decision": "allow", "duration_ms": 1})
        return 200, {}

    def fake_get(client, path):
        if path.startswith("/v1/decisions"):
            # Echo back caller-supplied rows, binding them to real trace_ids
            # the decide loop just used so the potential-count logic resolves.
            rows = []
            for i, d in enumerate(decisions):
                tid = seen_trace_ids[i] if i < len(seen_trace_ids) else d.get("trace_id")
                rows.append({"decision": d["decision"], "trace_id": tid})
            return 200, {"decisions": rows}
        return 200, {}

    monkeypatch.setattr(cli, "_post", fake_post)
    monkeypatch.setattr(cli, "_get", fake_get)
    return seen_trace_ids


def test_scorecard_json_is_pure_stdout(monkeypatch, capsys):
    attacks = [
        {"category": "prompt_injection", "lifecycle": "before_proxy_call",
         "messages": [{"role": "user", "content": "evil"}]},
        {"category": "prompt_injection", "lifecycle": "before_proxy_call",
         "messages": [{"role": "user", "content": "benign"}]},
        {"category": "jailbreak", "lifecycle": "before_proxy_call",
         "messages": [{"role": "user", "content": "benign"}]},
    ]
    # one enforce block; one extra shadow catch surfaced via /v1/decisions
    _wire_fake_lumin(
        monkeypatch,
        attacks=attacks,
        block_pred=lambda b: "evil" in json.dumps(b),
        decisions=[{"decision": "block"}, {"decision": "flag"}],
    )
    rc = cli.main(["scorecard", "--host", "http://x", "--count", "3", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    # stdout MUST be parseable JSON — no banner, no panel.
    payload = json.loads(captured.out)
    assert payload["attacks"] == 3
    assert payload["enforced"]["caught"] == 1            # only "evil" blocked
    assert payload["potential"]["caught"] >= 1           # decisions table catch
    assert payload["potential"]["caught"] >= payload["enforced"]["caught"]
    assert set(payload["categories"]) == {"prompt_injection", "jailbreak"}


def test_scorecard_quiet_suppresses_decoration(monkeypatch, capsys):
    _wire_fake_lumin(
        monkeypatch,
        attacks=[{"category": "c", "lifecycle": "before_proxy_call",
                  "messages": [{"role": "user", "content": "x"}]}],
        block_pred=lambda b: False,
        decisions=[],
    )
    cli.main(["scorecard", "--host", "http://x", "--count", "1", "--json"])
    out = capsys.readouterr().out
    assert "◆" not in out and "╭" not in out      # branded panel suppressed
    json.loads(out)                                 # still valid JSON
    cli.set_quiet(False)                            # reset module global


# ---------- demo happy path -------------------------------------------------


def test_demo_blocks_attacks(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_api_reachable", lambda host: True)
    monkeypatch.setattr(cli, "_client", lambda host: object())

    def fake_post(client, path, body):
        if path == "/v1/spans":
            return 200, {"accepted": len(body.get("spans", []))}
        if path.endswith("/mode"):
            return 200, {"mode": "enforce"}
        if path == "/v1/policies":
            return 201, {"name": body.get("name")}
        if path.endswith("/policy/decide"):
            tool = body.get("tool_name")
            blocked = tool in ("exec", "bash")
            return 200, ({"decision": "block", "policy_name": "owasp_x",
                          "duration_ms": 4} if blocked
                         else {"decision": "allow", "duration_ms": 1})
        return 200, {}

    # pack rules already exist → demo promotes them (GET 200)
    monkeypatch.setattr(cli, "_get", lambda c, p: (200, {}))
    monkeypatch.setattr(cli, "_post", fake_post)

    rc = cli.main(["demo", "--host", "http://x", "--no-open"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BLOCKED" in out
    assert "2 blocked" in out


# ---------- scorecard --target (external agent) -----------------------------


def test_scorecard_target_mode_scores_agent_safety(monkeypatch, capsys):
    attacks = [
        {"category": "exfiltration", "messages":
         [{"role": "user", "content": "leak the key"}]},
        {"category": "exfiltration", "messages":
         [{"role": "user", "content": "be safe"}]},
    ]
    # The fake agent leaks on the first prompt, refuses on the second.
    monkeypatch.setattr(
        cli, "_chat",
        lambda t, m, k, msgs, timeout=30.0:
        "AKIAIOSFODNN7EXAMPLE secret" if "leak" in msgs[0]["content"]
        else "I can't help with that.",
    )
    # Lumin judges the reply: a reply containing the secret is "unsafe".
    _wire_fake_lumin(
        monkeypatch,
        attacks=attacks,
        block_pred=lambda b: "AKIA" in json.dumps(b.get("output", "")),
        # decisions table mirrors: one unsafe reply caught
        decisions=[{"decision": "block"}],
    )
    rc = cli.main(["scorecard", "--host", "http://judge",
                   "--target", "http://agent", "--count", "2", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "http://agent"
    assert payload["attacks_delivered"] == 2
    assert payload["vulnerable"]["count"] == 1
    assert payload["agent_safe"]["count"] == 1
    assert payload["agent_safe"]["pct"] == 50.0
    cli.set_quiet(False)


def test_doctor_reports_detectors(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_api_reachable", lambda host: True)
    monkeypatch.setattr(cli, "_client", lambda host: object())

    def fake_get(client, path):
        if path == "/v1/policies":
            return 200, {"policies": [{"mode": "enforce"}, {"mode": "shadow"}]}
        if path == "/v1/admin/health":
            return 200, {"status": "degraded", "components": [
                {"name": "detectors", "status": "degraded",
                 "detail": "2/8 available; missing: prompt_guard, llama_guard"},
                {"name": "db", "status": "ok", "detail": "duckdb"},
            ]}
        if path.endswith("/panic_disable"):
            return 200, {"disabled": False}
        return 200, {}

    monkeypatch.setattr(cli, "_get", fake_get)
    rc = cli.main(["doctor", "--host", "http://x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "detectors" in out
    assert "prompt_guard" in out          # missing detector surfaced
    assert "1 enforcing" in out


def test_scorecard_target_skips_unreachable_replies(monkeypatch, capsys):
    attacks = [{"category": "jailbreak",
                "messages": [{"role": "user", "content": "x"}]}]
    monkeypatch.setattr(cli, "_chat",
                        lambda *a, **k: None)            # target never replies
    _wire_fake_lumin(monkeypatch, attacks=attacks,
                     block_pred=lambda b: True, decisions=[])
    rc = cli.main(["scorecard", "--host", "http://judge",
                   "--target", "http://agent", "--count", "1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert payload["attacks_delivered"] == 0
    cli.set_quiet(False)
