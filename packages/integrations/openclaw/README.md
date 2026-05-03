# @synaptic/openclaw

> Debug your [OpenClaw](https://github.com/openclaw/openclaw) agent locally. No account. No cloud. No telemetry leaving your laptop.

`@synaptic/openclaw` is the Synaptic side of OpenClaw's OTel telemetry pipeline. OpenClaw ships diagnostics through `@openclaw/diagnostics-otel`; this package provides a drop-in `SpanExporter` that converts those spans to Synaptic's wire format and ships them to a local Synaptic instance — visible at `http://localhost:3000`.

## Install

```bash
npm install @synaptic/openclaw
```

You also need a running Synaptic instance:

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/synaptic
```

## Usage — wired into OpenClaw's diagnostics

Build the processor and pass it to `@openclaw/diagnostics-otel`'s `spanProcessors:` array (or to any OTel `NodeTracerProvider` config you're already using):

```typescript
import { synapticProcessor } from '@synaptic/openclaw';

// In your OpenClaw / OTel config:
const tracerProvider = new NodeTracerProvider({
  resource,
  spanProcessors: [
    synapticProcessor({ serviceName: 'my-openclaw-bot' }),
    // …other processors
  ],
});
```

That's it. Run your OpenClaw agent, open `http://localhost:3000`, every LLM call, tool call, and channel session shows up live.

The helper reads `SYNAPTIC_HOST`, `SYNAPTIC_API_KEY`, and `SYNAPTIC_SERVICE_NAME` from the environment, so common deployments need no constructor args:

```bash
export SYNAPTIC_HOST=http://localhost:8000
```

## Usage — bare exporter

If you want full control over the OTel pipeline (e.g. `SimpleSpanProcessor` for tests), use the bare exporter:

```typescript
import { SynapticExporter } from '@synaptic/openclaw';
import { SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';

const exporter = new SynapticExporter({
  host: 'http://localhost:8000',
  project: 'my-bot',
});
const processor = new SimpleSpanProcessor(exporter);
```

## Usage — side-effect import (legacy OTel only)

For setups running an older OTel SDK whose `TracerProvider` still exposes a public `addSpanProcessor`:

```bash
export SYNAPTIC_TRACING=true
```

```typescript
import '@synaptic/openclaw/auto';
```

This path is best-effort — modern OTel (and OpenClaw's diagnostics-otel) requires processors to be supplied at construction time, in which case the import silently no-ops and you should fall back to `synapticProcessor()` above.

## What you get in the dashboard

- **Channel sessions** — every WhatsApp / Telegram / Discord / Slack / iMessage / etc. conversation as a session you can browse via the dashboard's `/sessions` view
- **LLM spans** — model, tokens, cost per call, with provider classification (OpenAI, Anthropic, Google, etc.) via standard OTel GenAI semantic conventions
- **Tool spans** — every external call (web search, GitHub, email, …) with input args and result, plus duration and error capture
- **Errors** — exception messages captured from OTel events, surfaced on the failing span in the timeline
- **Claude extended thinking** — if your OpenClaw agent uses Claude with thinking enabled, reasoning blocks render as first-class child spans

## Configuration

| Option | Env var | Default | Description |
|---|---|---|---|
| `host` | `SYNAPTIC_HOST` | `http://localhost:8000` | Synaptic API base URL |
| `apiKey` | `SYNAPTIC_API_KEY` | _none_ | Optional bearer token for hosted Synaptic |
| `project` | — | `openclaw` | Project tag (sent as `X-Synaptic-Project`) |
| `timeoutMs` | — | `5000` | Per-export network timeout |
| `serviceName` | `SYNAPTIC_SERVICE_NAME` | `openclaw-app` | OTel `service.name` resource attribute |

## Why local instead of Langfuse / Braintrust / Arize?

OpenClaw's whole pitch is "your agent on your devices, no cloud." Sending its telemetry to a SaaS observability backend defeats the point. Synaptic is the local-first alternative:

- **Air-gapped or sensitive data** — Synaptic runs entirely on your laptop or your VPC.
- **Zero account / zero billing** — clone the repo, `docker run`, done.
- **Apache 2.0** — fork it.

## Resilience

Per [Synaptic's Rule 7](https://github.com/amitbidlan/zistica-synaptic/blob/main/docs/Development_Rules.md), the agent **never fails because of Synaptic**. If the API is unreachable, returns 5xx, or the network hangs, the export reports success to the OTel pipeline and your agent keeps running. Spans drop silently.

## License

Apache-2.0.
