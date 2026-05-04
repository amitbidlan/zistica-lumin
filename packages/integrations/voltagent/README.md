# @lumin-io/voltagent

> Debug your [VoltAgent](https://github.com/VoltAgent/voltagent) agent locally. No account. No cloud. No telemetry leaving your laptop.

`@lumin-io/voltagent` is the Lumin side of VoltAgent's OTel telemetry pipeline. VoltAgent is OTel-native — it sets up an OTel TracerProvider and emits spans for every agent generation, LLM call, tool call, and guardrail. This package provides a drop-in `SpanExporter` that converts those spans to Lumin's wire format and ships them to a local Lumin instance — visible at `http://localhost:3000`.

## Install

```bash
npm install @lumin-io/voltagent
```

You also need a running Lumin instance:

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/lumin
```

## Usage

Build the processor and pass it to your VoltAgent OTel config (or to any `NodeTracerProvider` you've already set up):

```typescript
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { luminProcessor } from '@lumin-io/voltagent';

const tracerProvider = new NodeTracerProvider({
  resource,
  spanProcessors: [
    luminProcessor({ serviceName: 'my-voltagent-app' }),
    // …other processors
  ],
});
tracerProvider.register();
```

That's it. Run your VoltAgent app, open `http://localhost:3000`, every agent generation, LLM call, and tool call shows up live.

The helper reads `LUMIN_HOST`, `LUMIN_API_KEY`, and `LUMIN_SERVICE_NAME` from the environment, so common deployments need no constructor args:

```bash
export LUMIN_HOST=http://lumin.internal:8000
export LUMIN_PROJECT=my-bot
node ./your-voltagent-app.js
```

## What gets captured

- **LLM spans** with model, provider, tokens, and per-call cost (OpenAI / Anthropic / fine-tuned / provider-prefixed model names all supported)
- **Tool spans** with input args, output, errors — VoltAgent emits bare `tool.name` + `agent.tool.initiated`, both recognized
- **Reasoning blocks** — Claude / Gemini extended-thinking surfaces under `ai.response.reasoning` and renders with the brain-emoji subtype
- **Guardrail spans** — VoltAgent's `guardrail.*` namespace lands as custom spans with the policy decision visible
- **Sub-agent runs** — `agent.parent.id` is used as a session-grouping fallback so subagent timelines stay nested
- **Live span streaming** — new spans appear in the dashboard the moment they end, via WebSocket

## Resilience (Lumin Rule 7)

If Lumin is down or returns a 5xx, your VoltAgent process never sees an error. Spans drop silently, the agent keeps running. Verified against DNS failure, 401/500/timeout/hang/garbage-payload, and 1000-span batches — same resilience suite as the other Lumin integrations.

## Verified against real upstream emission

The exporter was built against the actual attribute keys `@voltagent/core` emits at runtime (verified via grep against the package's compiled source), not against the OTel GenAI semconv documentation in isolation. Specifically supported:

- `ai.model.name` / `ai.model.provider` / `ai.model.temperature` / `ai.model.max_tokens` (Vercel AI SDK convention)
- `ai.response.reasoning` (extended thinking)
- bare `input` / `output` (VoltAgent's content shape on root + LLM + tool spans)
- `voltagent.input` / `voltagent.output` / `voltagent.tool.name` / `voltagent.session_id` (explicit namespace)
- `agent.parent.id` (sub-agent session grouping)
- `agent.step.text` (intermediate step output)
- `agent.tool.initiated` (tool-call lifecycle hint)
- `gen_ai.*` (any framework using the OTel GenAI semconv)

If your VoltAgent app emits an attribute key the exporter doesn't pick up, [open an issue](https://github.com/amitbidlan/zistica-lumin/issues/new).

## License

Apache-2.0.
