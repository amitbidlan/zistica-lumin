# @lumin-io/openclaw-diagnostics

> Full-fidelity Lumin observability for [OpenClaw](https://github.com/openclaw/openclaw) — every prompt, response, tool I/O, and reasoning trace, captured per turn.

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
- Lightweight summary in metadata: `history_message_count`, `system_prompt_chars`, `images_count`

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
| `captureSystemPrompt` | `false` | Whether to write the full system prompt to `metadata.openclaw.content.system_prompt`. Off by default — system prompts are often large and rarely actionable. The character count is captured either way. |
| `maxContentChars` | `32768` | Per-attribute content cap. Truncated values are tagged `…(truncated)`. |
| `timeoutMs` | `5000` | HTTP timeout for the POST to Lumin. |

Example:

```json
{
  "plugins": {
    "entries": {
      "lumin-diagnostics": {
        "hooks": { "allowConversationAccess": true },
        "config": {
          "host": "http://my-lumin-host:8000",
          "captureSystemPrompt": true,
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

## Source + issues

- Repo: <https://github.com/amitbidlan/zistica-lumin/tree/main/packages/integrations/openclaw-diagnostics>
- Issues: <https://github.com/amitbidlan/zistica-lumin/issues>

## License

Apache 2.0
