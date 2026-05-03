# @synaptic/mastra

> Debug your Mastra agents locally. No account. No cloud. No telemetry leaving your laptop.

`@synaptic/mastra` plugs the Mastra agent framework into [Synaptic](https://github.com/amitbidlan/zistica-synaptic) — an open-source, local-first AI observability stack. Spans from your Mastra agents stream into a Synaptic dashboard at `http://localhost:3000` with full timeline, token counts, cost, and tool-call inspection.

## Install

```bash
npm install @synaptic/mastra
```

You also need a running Synaptic instance:

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/synaptic
```

## Usage — explicit (recommended for Mastra)

Mastra builds its OTel tracer provider during `new Mastra(...)`. Modern OpenTelemetry doesn't allow attaching span processors after construction, so the supported way to wire Synaptic into Mastra is the `observability.configs.*.exporters` array — a one-liner via `synapticConfig()`:

```typescript
import { Mastra } from '@mastra/core';
import { synapticConfig } from '@synaptic/mastra';

export const mastra = new Mastra({
  agents: { myAgent },
  observability: synapticConfig({ serviceName: 'my-mastra-app' }),
});
```

The helper reads `SYNAPTIC_HOST`, `SYNAPTIC_API_KEY`, and `SYNAPTIC_SERVICE_NAME` from the environment, so the rest is just:

```bash
export SYNAPTIC_HOST=http://localhost:8000
```

Run your agent, open `http://localhost:3000`, the trace appears.

## Usage — fully manual

If you want to control every option:

```typescript
import { Mastra } from '@mastra/core';
import { SynapticExporter } from '@synaptic/mastra';

export const mastra = new Mastra({
  agents: { myAgent },
  observability: {
    configs: {
      synaptic: {
        serviceName: 'my-mastra-app',
        exporters: [
          new SynapticExporter({
            host: 'http://localhost:8000', // default
          }),
        ],
      },
    },
  },
});
```

## Usage — side-effect import (legacy OTel only)

For frameworks running on older OTel SDK versions whose `TracerProvider` still exposes a public `addSpanProcessor`, you can opt in with a single import line and an env var:

```bash
export SYNAPTIC_TRACING=true
```

```typescript
import '@synaptic/mastra/auto';
```

This path is best-effort — if the runtime uses modern OTel (where processors must be set at provider construction), the install silently no-ops and you should fall back to `synapticConfig()` above. Mastra v1+ is the modern-OTel case.

## What you get in the dashboard

- **Timeline view** — every Mastra agent run, agent step, tool call, and LLM request as nested spans
- **Tokens & cost** — per-LLM-span via OTel GenAI semantic conventions (`gen_ai.usage.*`, `gen_ai.request.model`)
- **Errors** — exception messages captured from OTel events
- **Sessions** — multi-turn agent conversations grouped automatically when `session.id` or `gen_ai.conversation.id` is set
- **Claude extended thinking** — if you use Claude with thinking enabled, reasoning blocks render as first-class child spans

## Configuration

| Option | Env var | Default | Description |
|---|---|---|---|
| `host` | `SYNAPTIC_HOST` | `http://localhost:8000` | Synaptic API base URL |
| `apiKey` | `SYNAPTIC_API_KEY` | _none_ | Optional bearer token for hosted Synaptic |
| `project` | — | `mastra` | Project tag (sent as `X-Synaptic-Project`) |
| `timeoutMs` | — | `5000` | Per-export network timeout |
| `serviceName` | `SYNAPTIC_SERVICE_NAME` | `mastra-app` | Mastra observability service name |

## Why not Langfuse / Braintrust / Arize?

Those are great if you're OK shipping every prompt and response to a SaaS. Synaptic is for when you can't or don't want to:

- **Air-gapped or sensitive data** — Synaptic runs entirely on your laptop or your VPC.
- **Zero account / zero billing** — clone the repo, `docker run`, done.
- **Apache 2.0** — fork it.
- **Same Mastra-side ergonomics** — `@synaptic/mastra` mirrors `@mastra/langfuse` so the swap is one line.

## Resilience

Per [Synaptic's Rule 7](https://github.com/amitbidlan/zistica-synaptic/blob/main/docs/Development_Rules.md), the agent **never fails because of Synaptic**. If the API is unreachable, returns 5xx, or the network hangs, the export reports success to the OTel pipeline and your agent keeps running. Spans drop silently.

## License

Apache-2.0.
