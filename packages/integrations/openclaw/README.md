# @lumin-io/openclaw

> Debug your [OpenClaw](https://github.com/openclaw/openclaw) agent locally. No account. No cloud. No telemetry leaving your laptop.

`@lumin-io/openclaw` is the Lumin side of OpenClaw's OTel telemetry pipeline. OpenClaw ships diagnostics through `@openclaw/diagnostics-otel`; this package provides a drop-in `SpanExporter` that converts those spans to Lumin's wire format and ships them to a local Lumin instance — visible at `http://localhost:3000`.

## Quick Start

Works out of the box with **OpenClaw v2026.2+** — no npm install, no agent code changes. Lumin exposes an OTLP/HTTP endpoint that OpenClaw's built-in `diagnostics-otel` plugin can write to directly.

```bash
# Step 1 — start Lumin
docker run -p 3000:3000 -p 8000:8000 zistica/lumin

# Step 2 — enable diagnostics + point OpenClaw at Lumin
openclaw config set diagnostics.enabled true
openclaw config set diagnostics.otel.enabled true
openclaw config set diagnostics.otel.traces true
openclaw config set diagnostics.otel.endpoint "http://localhost:8000/v1/otlp"
openclaw gateway restart

# Step 3 — open http://localhost:3000
# every OpenClaw run appears automatically
```

OpenClaw's `diagnostics-otel` auto-appends `/v1/traces` to the configured base, so the POST lands at Lumin's OTLP route `http://localhost:8000/v1/otlp/v1/traces`. On v2026.4.25+ you can use the signal-specific form instead:

```bash
openclaw config set diagnostics.otel.traces.endpoint "http://localhost:8000/v1/otlp/v1/traces"
```

**What gets captured**
- LLM calls — model, tokens, cost, duration
- Tool calls — file, shell, web, email
- Agent sessions end-to-end as a span tree
- Policy violations auto-detected by Lumin's [policy engine](../../../README.md#policy-engine)

When to install the `@lumin-io/openclaw` npm package below: you want client-side cost calculation, custom span subtypes (e.g. extended-thinking), or you're not using OpenClaw's built-in `diagnostics-otel` plugin.

## Install

```bash
npm install @lumin-io/openclaw
```

You also need a running Lumin instance:

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/lumin
```

## Usage — wired into OpenClaw's diagnostics

Build the processor and pass it to `@openclaw/diagnostics-otel`'s `spanProcessors:` array (or to any OTel `NodeTracerProvider` config you're already using):

```typescript
import { luminProcessor } from '@lumin-io/openclaw';

// In your OpenClaw / OTel config:
const tracerProvider = new NodeTracerProvider({
  resource,
  spanProcessors: [
    luminProcessor({ serviceName: 'my-openclaw-bot' }),
    // …other processors
  ],
});
```

That's it. Run your OpenClaw agent, open `http://localhost:3000`, every LLM call, tool call, and channel session shows up live.

The helper reads `LUMIN_HOST`, `LUMIN_API_KEY`, and `LUMIN_SERVICE_NAME` from the environment, so common deployments need no constructor args:

```bash
export LUMIN_HOST=http://localhost:8000
```

## Usage — bare exporter

If you want full control over the OTel pipeline (e.g. `SimpleSpanProcessor` for tests), use the bare exporter:

```typescript
import { LuminExporter } from '@lumin-io/openclaw';
import { SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';

const exporter = new LuminExporter({
  host: 'http://localhost:8000',
  project: 'my-bot',
});
const processor = new SimpleSpanProcessor(exporter);
```

## Usage — side-effect import (legacy OTel only)

For setups running an older OTel SDK whose `TracerProvider` still exposes a public `addSpanProcessor`:

```bash
export LUMIN_TRACING=true
```

```typescript
import '@lumin-io/openclaw/auto';
```

This path is best-effort — modern OTel (and OpenClaw's diagnostics-otel) requires processors to be supplied at construction time, in which case the import silently no-ops and you should fall back to `luminProcessor()` above.

## What you get in the dashboard

- **Channel sessions** — every WhatsApp / Telegram / Discord / Slack / iMessage / etc. conversation as a session you can browse via the dashboard's `/sessions` view
- **LLM spans** — model, tokens, cost per call, with provider classification (OpenAI, Anthropic, Google, etc.) via standard OTel GenAI semantic conventions
- **Tool spans** — every external call (web search, GitHub, email, …) with input args and result, plus duration and error capture
- **Errors** — exception messages captured from OTel events, surfaced on the failing span in the timeline
- **Claude extended thinking** — if your OpenClaw agent uses Claude with thinking enabled, reasoning blocks render as first-class child spans

## Configuration

| Option | Env var | Default | Description |
|---|---|---|---|
| `host` | `LUMIN_HOST` | `http://localhost:8000` | Lumin API base URL |
| `apiKey` | `LUMIN_API_KEY` | _none_ | Optional bearer token for hosted Lumin |
| `project` | — | `openclaw` | Project tag (sent as `X-Lumin-Project`) |
| `timeoutMs` | — | `5000` | Per-export network timeout |
| `serviceName` | `LUMIN_SERVICE_NAME` | `openclaw-app` | OTel `service.name` resource attribute |

## Why local instead of Langfuse / Braintrust / Arize?

OpenClaw's whole pitch is "your agent on your devices, no cloud." Sending its telemetry to a SaaS observability backend defeats the point. Lumin is the local-first alternative:

- **Air-gapped or sensitive data** — Lumin runs entirely on your laptop or your VPC.
- **Zero account / zero billing** — clone the repo, `docker run`, done.
- **Apache 2.0** — fork it.

## Resilience

Per [Lumin's Rule 7](https://github.com/amitbidlan/zistica-lumin/blob/main/docs/Development_Rules.md), the agent **never fails because of Lumin**. If the API is unreachable, returns 5xx, or the network hangs, the export reports success to the OTel pipeline and your agent keeps running. Spans drop silently.

## License

Apache-2.0.
