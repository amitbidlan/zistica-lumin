# @lumin-io/mastra

> Debug your Mastra agents locally. No account. No cloud. No telemetry leaving your laptop.

`@lumin-io/mastra` plugs the Mastra agent framework into [Lumin](https://github.com/amitbidlan/zistica-lumin) — an open-source, local-first AI observability stack. Spans from your Mastra agents stream into a Lumin dashboard at `http://localhost:3000` with full timeline, token counts, cost, and tool-call inspection.

## Install

```bash
npm install @lumin-io/mastra
```

You also need a running Lumin instance:

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/lumin
```

## Usage — explicit (recommended for Mastra)

Mastra builds its OTel tracer provider during `new Mastra(...)`. Modern OpenTelemetry doesn't allow attaching span processors after construction, so the supported way to wire Lumin into Mastra is the `observability.configs.*.exporters` array — a one-liner via `luminConfig()`:

```typescript
import { Mastra } from '@mastra/core';
import { luminConfig } from '@lumin-io/mastra';

export const mastra = new Mastra({
  agents: { myAgent },
  observability: luminConfig({ serviceName: 'my-mastra-app' }),
});
```

The helper reads `LUMIN_HOST`, `LUMIN_API_KEY`, and `LUMIN_SERVICE_NAME` from the environment, so the rest is just:

```bash
export LUMIN_HOST=http://localhost:8000
```

Run your agent, open `http://localhost:3000`, the trace appears.

## Usage — fully manual

If you want to control every option:

```typescript
import { Mastra } from '@mastra/core';
import { LuminExporter } from '@lumin-io/mastra';

export const mastra = new Mastra({
  agents: { myAgent },
  observability: {
    configs: {
      lumin: {
        serviceName: 'my-mastra-app',
        exporters: [
          new LuminExporter({
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
export LUMIN_TRACING=true
```

```typescript
import '@lumin-io/mastra/auto';
```

This path is best-effort — if the runtime uses modern OTel (where processors must be set at provider construction), the install silently no-ops and you should fall back to `luminConfig()` above. Mastra v1+ is the modern-OTel case.

## Usage — Agent Firewall

Wrap any Mastra Tool with `wrapToolWithFirewall` to add synchronous policy enforcement. Every tool invocation hits Lumin's `/v1/policy/decide` endpoint before executing — the response can `allow`, `block`, `rewrite` (substitute params), or `require_approval` (long-poll until an operator decides).

```typescript
import { createTool } from '@mastra/core';
import { wrapToolWithFirewall } from '@lumin-io/mastra';

const shellTool = createTool({
  id: 'shell',
  description: 'Run a shell command',
  execute: async ({ context }) => runCommand(context.command),
});

export const guardedShell = wrapToolWithFirewall(shellTool, {
  host: 'http://localhost:8000',
  project: 'my-bot',
  // Admin separation — Slice 2 Tier 1.0
  adminSenders: ['telegram:5706212396'],
  onFirewallError: 'allow',  // Rule 7 — agent never blocks on Lumin
});
```

When the firewall blocks, the wrapper throws `FirewallBlockedError` with a sender-aware message: admins see the full reasoning (policy name, reason, agent_feedback), non-admins get a generic "contact your administrator" line that closes the social-engineering surface.

For lower-level integrations, `LuminFirewallClient` exposes `decide()` and `waitForApproval()` directly.

## What you get in the dashboard

- **Timeline view** — every Mastra agent run, agent step, tool call, and LLM request as nested spans
- **Tokens & cost** — per-LLM-span via OTel GenAI semantic conventions (`gen_ai.usage.*`, `gen_ai.request.model`)
- **Errors** — exception messages captured from OTel events
- **Sessions** — multi-turn agent conversations grouped automatically when `session.id` or `gen_ai.conversation.id` is set
- **Claude extended thinking** — if you use Claude with thinking enabled, reasoning blocks render as first-class child spans
- **Firewall decisions** — every wrapped-tool call appears in the EnforcementTimeline with its decision verb, mode, and policy name

## Configuration

| Option | Env var | Default | Description |
|---|---|---|---|
| `host` | `LUMIN_HOST` | `http://localhost:8000` | Lumin API base URL |
| `apiKey` | `LUMIN_API_KEY` | _none_ | Optional bearer token for hosted Lumin |
| `project` | — | `mastra` | Project tag (sent as `X-Lumin-Project`) |
| `timeoutMs` | — | `5000` | Per-export network timeout |
| `serviceName` | `LUMIN_SERVICE_NAME` | `mastra-app` | Mastra observability service name |

## Why not Langfuse / Braintrust / Arize?

Those are great if you're OK shipping every prompt and response to a SaaS. Lumin is for when you can't or don't want to:

- **Air-gapped or sensitive data** — Lumin runs entirely on your laptop or your VPC.
- **Zero account / zero billing** — clone the repo, `docker run`, done.
- **Apache 2.0** — fork it.
- **Same Mastra-side ergonomics** — `@lumin-io/mastra` mirrors `@mastra/langfuse` so the swap is one line.

## Resilience

Per [Lumin's Rule 7](https://github.com/amitbidlan/zistica-lumin/blob/main/docs/Development_Rules.md), the agent **never fails because of Lumin**. If the API is unreachable, returns 5xx, or the network hangs, the export reports success to the OTel pipeline and your agent keeps running. Spans drop silently.

## License

Apache-2.0.
