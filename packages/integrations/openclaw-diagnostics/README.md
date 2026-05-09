# @lumin-io/openclaw-diagnostics

> Full-fidelity Lumin observability **and synchronous policy enforcement** for [OpenClaw](https://github.com/openclaw/openclaw) — every prompt, response, tool I/O, and reasoning trace captured per turn, every tool call checked against your firewall before it runs.

**v0.5.3 — Lumin replies, not the LLM.** When the firewall blocks at the input boundary (prompt-injection attempt, training-data extraction, etc.), Lumin now **takes over the reply** entirely. The LLM still runs (OpenClaw's plugin SDK doesn't expose a hook that can cancel the model call) but its output is discarded — the user sees the operator's `userInputBlockedMessage` instead. Closes three attacker-visible leaks: LLM-generated refusals exposing rule names, fake `/approve` hallucinations, and inconsistent refusal phrasing. Default on; opt out with `replyOnInputBlock: false`.

**v0.5.1 added LLM-side firewall hooks.** `before_prompt_build` calls `decide(before_proxy_call)`, `before_agent_reply` calls `decide(after_proxy_call)`. Together with the existing tool-call enforcement, every OWASP LLM Top 10 lifecycle is now reachable.

**v0.4.0 adds admin separation.** Configure `adminSenders` to keep approval prompts and LLM technical detail away from end users — non-admin senders get a clean canned message after a block, admins get the full context. The plugin enforces this via OpenClaw's `inbound_claim` and `before_message_write` hooks. v0.2.0 introduced the Agent Firewall: every `before_tool_call` POSTs to Lumin's `/v1/policy/decide` endpoint and translates the response into OpenClaw's typed-hook return contract — `block`, `rewrite`, or `requireApproval`. Set `enforce: false` to fall back to observation-only.

[![npm](https://img.shields.io/npm/v/@lumin-io/openclaw-diagnostics.svg?style=flat-square)](https://www.npmjs.com/package/@lumin-io/openclaw-diagnostics)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg?style=flat-square)](https://github.com/amitbidlan/zistica-lumin/blob/main/LICENSE)

OpenClaw runs ship native OpenTelemetry through the bundled [`diagnostics-otel`](https://docs.openclaw.ai/gateway/opentelemetry) plugin. That gets you provider, model, token counts, and span structure — but **not the actual prompt or reply text**. The plugin exposes a `captureContent` flag, but as of OpenClaw 2026.5.x the runtime never populates the underlying event fields the exporter tries to read, so the flag is effectively a no-op.

This plugin uses a different surface — OpenClaw's typed-hook API (`api.on('llm_input', …)` / `api.on('llm_output', …)`) — which **does** carry full content at runtime. On every turn it builds a Lumin `SpanInput` and POSTs it to your local Lumin instance. The agent never blocks on Lumin; failures are swallowed with a short timeout.

## What you get

Per LLM turn:

- Prompt text (just this turn's user message — not the whole replayed history)
- Assistant reply text
- **Reasoning trace** for thinking-emitting models (gpt-oss, o-series, Claude with extended thinking) — surfaced under `metadata.openclaw.content.thinking`
- Token usage (input / output)
- Model + provider + harness ID
- Trace ID stitched from OpenClaw's diagnostic context (so the typed-hook span fuses with whatever else you've ingested via OTel for the same run)
- Lightweight summary in metadata: `history_message_count`, `system_message_chars`, `images_count`

Per tool call:

- Tool name + invocation params (input)
- Tool result (output) — when the tool succeeds
- Error message — when the tool fails
- Duration in ms
- `toolCallId` and `runId` correlation in metadata
- Same `trace_id` as the LLM span for the same run, so the dashboard renders the LLM call and its tool invocations as a single trace timeline

## Installation

```bash
openclaw plugins install @lumin-io/openclaw-diagnostics
```

Then enable conversation access for the plugin in your `~/.openclaw/openclaw.json` — non-bundled plugins are conversation-gated by default, so without this the typed hooks register silently and you'll see nothing:

```json
{
  "plugins": {
    "entries": {
      "lumin-diagnostics": {
        "hooks": { "allowConversationAccess": true }
      }
    }
  }
}
```

Restart the gateway:

```bash
openclaw daemon restart
```

You should see this line in the gateway log on startup:

```
lumin-diagnostics: subscribed to llm_input + llm_output → http://localhost:8000/v1/spans (project=openclaw)
```

## Configuration

All optional, set under `plugins.entries.lumin-diagnostics.config` in `openclaw.json`:

| Key | Default | Description |
|---|---|---|
| `host` | `http://localhost:8000` | Lumin API base URL. Set this in your `openclaw.json` to point at a Lumin instance running anywhere other than the default localhost (e.g. `http://lumin.internal:8000`, `http://host.docker.internal:8000`). |
| `project` | `openclaw` | Sent as `X-Lumin-Project` so the agent grid groups OpenClaw runs together. |
| `captureSystemMessage` | `false` | Whether to write the full system message text to `metadata.openclaw.content.system_message`. Off by default — these payloads are often large and rarely actionable for debugging. The character count is captured into metadata either way. |
| `maxContentChars` | `32768` | Per-attribute content cap. Truncated values are tagged `…(truncated)`. |
| `timeoutMs` | `5000` | HTTP timeout for the POST to Lumin. |
| `enforce` | `true` | **(v0.2.0)** Whether to call `/v1/policy/decide` synchronously on every `before_tool_call`. Set to `false` to fall back to observation-only behavior. |
| `decideTimeoutMs` | `75` | **(v0.2.0)** Hard timeout for the synchronous decide call. Tighter than `timeoutMs` because every tool call pays this latency. |
| `onFirewallError` | `"allow"` | **(v0.2.0)** Fail-mode when the firewall is unreachable: `"allow"` (Rule 7 default) lets the tool run, `"deny"` cancels it. |
| `adminSenders` | `[]` | **(v0.4.0)** Sender IDs treated as administrators. Format: `telegram:5706212396`, `slack:U02ABCD123`, `whatsapp:+1234567890` — whatever OpenClaw produces as the canonical channel-scoped sender. Empty (default) means every sender is non-admin. |
| `userBlockedMessage` | *(neutral default)* | **(v0.4.0)** What end users see when the LLM tries to reply after a Lumin block. Defaults to: *"I'm unable to perform that action due to security policy. Please contact your administrator if you need assistance."* |
| `adminSeesFullResponse` | `true` | **(v0.4.0)** Whether admin senders see the agent's full reply (including any policy detail the LLM mentions) after a block. Set `false` to suppress for admins too — useful when ALL admin context should route through the Lumin dashboard. |

Example:

```json
{
  "plugins": {
    "entries": {
      "lumin-diagnostics": {
        "hooks": { "allowConversationAccess": true },
        "config": {
          "host": "http://my-lumin-host:8000",
          "captureSystemMessage": true,
          "maxContentChars": 65536
        }
      }
    }
  }
}
```

## Why a plugin instead of just OTel?

Two reasons:

1. **Content fidelity.** The OTel exporter ships fine for structure + sizes but doesn't get prompts or replies through (see the upstream bug note above). The typed-hook API does, and stays compatible across OpenClaw releases.
2. **Composability.** This plugin runs *alongside* `diagnostics-otel` — both can be enabled at the same time. OTel ships your spans to Honeycomb / Datadog / etc. with structure + sizes; this plugin ships full-content spans to your local Lumin for debugging. They don't conflict.

## Compatibility

- **OpenClaw**: ≥ 2026.4.25 (uses the typed-hook API surface). Older releases that predate `api.on(...)` register silently with a single warn line — no errors.
- **Lumin API**: any version with `POST /v1/spans` — i.e. all current versions.

## Caveats

- **Trace ID stitching.** OpenClaw's typed hooks and its OTel exporter sometimes run under different `runWithDiagnosticTraceContext` envelopes, so the trace IDs Lumin sees from the two rails *may* not match. The plugin always emits a deterministic trace ID derived from the OpenClaw `runId`, so two ingests of the same run idempotently land on the same trace.
- **History is summarized, not embedded.** Each turn captures only the current user prompt — the full conversation history (which OpenClaw replays to the model on every turn) is referenced by count, not embedded, so trace size doesn't grow linearly with conversation length. If you need the full conversation, use Lumin's `/sessions` view, which already groups turns by `session_id`.

## Changelog

### 0.4.0 — Admin separation + LLM feedback

Closes the social-engineering surface where the LLM hallucinates a fake `/approve <command>` syntax for the user to click — captured live during Slice 1 dogfood (2026-05-07).

- **`adminSenders` config:** list of OpenClaw-canonical sender IDs (e.g. `telegram:5706212396`, `slack:U02ABCD123`) treated as administrators. Non-admin senders get a canned message after a Lumin block; the LLM's reply is intercepted via the new `before_message_write` hook.
- **`userBlockedMessage` config:** override the default user-facing canned message.
- **`adminSeesFullResponse` config (default `true`):** admins see the agent's full reply with policy detail; set `false` to route admin context only through the Lumin dashboard.
- **New hooks registered:** `inbound_claim` (records sessionKey → senderId) and `before_message_write` (intercepts non-admin replies after recent block).
- **Surfaces `agent_feedback` from Lumin's decide response** as the tool error string verbatim — authoritative, anti-hallucination, anti-retry text targeted at the LLM's reasoning trace.

Behavior change vs 0.3.x: previously, every sender (including end users) saw OpenClaw's native `🛡️ Plugin approval required` prompt and could `/approve` it themselves — a real bypass. With `adminSenders` configured, end users never see the approval surface.

### 0.3.0 — Lockstep version alignment

Version bump to align with Lumin platform v0.3.0 (Lumin API Docker, all SDKs and integrations). No functional changes from 0.2.0.

### 0.2.0 — Agent Firewall enforcement

Adds a synchronous policy enforcement layer on top of the v0.1.x observation flow. Every `before_tool_call` hook now POSTs to Lumin's `/v1/policy/decide` endpoint and translates the decision into OpenClaw's typed-hook return contract.

- **Mapping:** Lumin `block` → `{ block: true, blockReason }`; `rewrite` → `{ params: rewritten.params }`; `require_approval` → `{ requireApproval: { ..., onResolution } }` that POSTs the operator's decision back to `/v1/approvals/{id}/resolve`; `flag` and `allow` → tool proceeds.
- **New config:** `enforce` (default `true` — set to `false` to revert to v0.1.x observation-only), `decideTimeoutMs` (default `75` — tighter than the 5000ms span timeout because every tool call pays this latency), `onFirewallError` (default `"allow"` — Rule 7 fail-mode; flip to `"deny"` for fail-closed deployments).
- **OpenClaw plugin lifecycle hooks:** `before_tool_call` and `after_tool_call` (in addition to `llm_input` / `llm_output` from v0.1.x).
- **Approval round-trip:** OpenClaw's native approval UI now correlates with Lumin's `/v1/approvals` table — operators get a full audit trail across both systems. `allow-once` / `allow-always` map to `allow`; `deny` / `cancelled` / `timeout` map to `deny`.
- **Tests:** 22 (12 existing + 10 new firewall cases).

### 0.1.x

Observation-only: prompts, replies, tool I/O, reasoning traces captured to Lumin via `/v1/spans`. See git history for sub-version details.

## Source + issues

- Repo: <https://github.com/amitbidlan/zistica-lumin/tree/main/packages/integrations/openclaw-diagnostics>
- Issues: <https://github.com/amitbidlan/zistica-lumin/issues>

## License

Apache 2.0
