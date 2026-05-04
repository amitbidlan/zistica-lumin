/**
 * Lumin exporter for VoltAgent and any other OTel-based agent framework.
 *
 * VoltAgent (`@voltagent/core`) is an OTel-native framework: it sets up
 * an OTel TracerProvider and emits spans for every agent generation,
 * LLM call, tool call, and guardrail. This exporter plugs into that
 * pipeline and converts each batch of OTel spans into Lumin's native
 * JSON span format, POSTing to `{host}/v1/spans`.
 *
 * VoltAgent attribute conventions verified against
 * `node_modules/@voltagent/core/dist/*.js`:
 *
 *   - `ai.model.name` / `ai.model.provider` — LLM identity
 *   - bare `input` / `output` — root + LLM + tool span content (this
 *     is the unusual one — most frameworks namespace these keys)
 *   - `agent.*` — lifecycle events (generation.started/completed,
 *     tool.initiated, summary.*)
 *   - `guardrail.*` — safety/policy spans
 *
 * Why not OTLP protobuf? Lumin's API exposes `/v1/spans` (native
 * JSON) as the documented ingest path. Routing through native JSON
 * keeps this package self-contained: no protobuf runtime, no schema
 * codegen — minimal install footprint.
 *
 * Resilience (Lumin Rule 7): the agent must never fail because
 * Lumin is unreachable. All network errors are swallowed; export()
 * always reports success to the OTel pipeline so the SDK doesn't
 * retry indefinitely or surface errors to user code.
 */

import type {
  ReadableSpan,
  SpanExporter,
} from '@opentelemetry/sdk-trace-base';
import { ExportResultCode, type ExportResult } from '@opentelemetry/core';
import { SpanKind, SpanStatusCode } from '@opentelemetry/api';

export interface LuminExporterConfig {
  /** Lumin API base URL. Defaults to env LUMIN_HOST or http://localhost:8000. */
  host?: string;
  /** Optional API key for hosted Lumin (sent as Authorization: Bearer). */
  apiKey?: string;
  /** Project tag for grouping. Defaults to "voltagent". */
  project?: string;
  /** Per-export network timeout in milliseconds. Default 5_000. */
  timeoutMs?: number;
  /** Inject a custom fetch impl — useful for tests. */
  fetchImpl?: typeof fetch;
  /** Maximum size for serialized input/output payloads. Default 10_240. */
  maxPayloadSize?: number;
}

interface LuminSpan {
  id: string;
  trace_id: string;
  parent_span_id: string | null;
  name: string;
  type: string;
  input: string | null;
  output: string | null;
  started_at: string;
  ended_at: string | null;
  error: string | null;
  session_id: string | null;
  span_subtype?: string | null;
  thinking_tokens?: number | null;
  model?: string | null;
  provider?: string | null;
  tokens_input?: number | null;
  tokens_output?: number | null;
  cost_usd?: number | null;
  tool_name?: string | null;
  metadata?: Record<string, unknown> | null;
}

const DEFAULT_HOST = 'http://localhost:8000';
const DEFAULT_TIMEOUT_MS = 5_000;
const DEFAULT_PROJECT = 'voltagent';
const DEFAULT_MAX_PAYLOAD = 10_240;

/**
 * Per-1k-token USD prices — same table as `@lumin-io/openclaw` and
 * `@lumin-io/mastra` so cost numbers stay consistent across frameworks.
 * Longest-prefix match against the (lowercased) model name.
 */
const PRICES_PER_1K: Record<string, [number, number]> = {
  'gpt-4o-mini': [0.00015, 0.0006],
  'gpt-4o': [0.0025, 0.010],
  'gpt-4-turbo': [0.010, 0.030],
  'gpt-4': [0.030, 0.060],
  'gpt-3.5-turbo': [0.0005, 0.0015],
  'claude-opus-4': [0.015, 0.075],
  'claude-sonnet-4': [0.003, 0.015],
  'claude-haiku-4': [0.001, 0.005],
  'text-embedding-3-small': [0.00002, 0],
  'text-embedding-3-large': [0.00013, 0],
  'text-embedding-ada-002': [0.0001, 0],
};

function normalizeModel(model: string): string {
  const m = model.toLowerCase();
  if (m.startsWith('ft:')) {
    const rest = m.slice(3);
    return rest.includes(':') ? rest.slice(0, rest.indexOf(':')) : rest;
  }
  if (m.includes('/')) return m.slice(m.indexOf('/') + 1);
  return m;
}

function computeCost(
  model: string | null | undefined,
  tin: number | null | undefined,
  tout: number | null | undefined,
): number | null {
  if (!model || tin == null || tout == null) return null;
  const m = normalizeModel(model);
  let bestKey = '';
  let best: [number, number] | null = null;
  for (const [key, prices] of Object.entries(PRICES_PER_1K)) {
    if (m.startsWith(key) && key.length > bestKey.length) {
      bestKey = key;
      best = prices;
    }
  }
  if (!best) return null;
  const [inp, outp] = best;
  return Math.round(((tin * inp) / 1000 + (tout * outp) / 1000) * 1e8) / 1e8;
}

export function registerModelPrice(
  modelPrefix: string,
  inputPer1k: number,
  outputPer1k: number,
): void {
  PRICES_PER_1K[modelPrefix.toLowerCase()] = [inputPer1k, outputPer1k];
}

function serialize(value: unknown, maxSize: number): string | null {
  if (value === null || value === undefined) return null;
  let s: string;
  if (typeof value === 'string') {
    s = value;
  } else if (Array.isArray(value)) {
    s = value
      .map((v) => (typeof v === 'string' ? v : safeStringify(v)))
      .join('\n');
  } else if (
    typeof value === 'number' ||
    typeof value === 'boolean' ||
    typeof value === 'bigint'
  ) {
    s = String(value);
  } else {
    s = safeStringify(value);
  }
  return s.length > maxSize ? s.slice(0, maxSize) : s;
}

function safeStringify(v: unknown): string {
  try {
    const result = JSON.stringify(v);
    return result === undefined ? String(v) : result;
  } catch {
    return String(v);
  }
}

/** OTel hr_time → microsecond-precision ISO-8601 in UTC. */
function hrTimeToIso(time: [number, number] | undefined | null): string | null {
  if (!time) return null;
  const [seconds, nanos] = time;
  const ms = seconds * 1000 + Math.floor(nanos / 1_000_000);
  const micros = Math.floor(nanos / 1_000) % 1_000_000;
  const isoMs = new Date(ms).toISOString();
  return isoMs.slice(0, -5) + '.' + String(micros).padStart(6, '0') + 'Z';
}

/**
 * Map an OTel span to a Lumin span `type`. VoltAgent emits a mix of
 * GenAI semconv keys, the Vercel AI SDK `ai.*` namespace, and its own
 * bare-attribute conventions.
 */
function classifySpanType(span: ReadableSpan): string {
  const attrs = span.attributes ?? {};
  // VoltAgent flags LLM spans either via OTel semconv OR via its
  // own `ai.model.name` (the Vercel AI SDK convention it inherits).
  if (
    attrs['gen_ai.operation.name'] ||
    attrs['gen_ai.request.model'] ||
    attrs['ai.model.name']
  ) {
    return 'llm';
  }
  // Tool detection: GenAI semconv key, the bare `tool.name` (used by
  // VoltAgent + Vercel AI SDK + many ad-hoc OTel pipelines), or the
  // VoltAgent-specific lifecycle hint.
  if (
    attrs['gen_ai.tool.name'] ||
    attrs['gen_ai.tool.type'] ||
    attrs['tool.name'] ||
    attrs['voltagent.tool.name'] ||
    attrs['agent.tool.initiated']
  ) {
    return 'tool';
  }
  switch (span.kind) {
    case SpanKind.CLIENT:
    case SpanKind.PRODUCER:
      return 'llm';
    case SpanKind.SERVER:
    case SpanKind.CONSUMER:
      return 'tool';
    case SpanKind.INTERNAL:
    default:
      return 'custom';
  }
}

function attrString(
  attrs: Record<string, unknown>,
  key: string,
): string | null {
  const v = attrs[key];
  return typeof v === 'string' ? v : null;
}

function attrNumber(
  attrs: Record<string, unknown>,
  key: string,
): number | null {
  const v = attrs[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Convert one OTel ReadableSpan to a Lumin SpanInput. */
export function otelSpanToLumin(
  span: ReadableSpan,
  maxPayloadSize: number = DEFAULT_MAX_PAYLOAD,
): LuminSpan {
  const ctx = span.spanContext();
  const attrs = (span.attributes ?? {}) as Record<string, unknown>;
  const resourceAttrs = ((span.resource as { attributes?: Record<string, unknown> })?.attributes ?? {}) as Record<string, unknown>;

  const luminType = classifySpanType(span);
  // VoltAgent emits `ai.model.name` and `ai.model.provider`. Other
  // frameworks emit `gen_ai.*`. Read both, in priority order.
  const model =
    attrString(attrs, 'gen_ai.response.model') ??
    attrString(attrs, 'gen_ai.request.model') ??
    attrString(attrs, 'ai.model.name') ??
    attrString(attrs, 'ai.model.id');
  const provider =
    attrString(attrs, 'gen_ai.system') ??
    attrString(attrs, 'ai.model.provider');
  const tokensIn =
    attrNumber(attrs, 'gen_ai.usage.input_tokens') ??
    attrNumber(attrs, 'gen_ai.usage.prompt_tokens');
  const tokensOut =
    attrNumber(attrs, 'gen_ai.usage.output_tokens') ??
    attrNumber(attrs, 'gen_ai.usage.completion_tokens');
  const toolName =
    attrString(attrs, 'gen_ai.tool.name') ??
    attrString(attrs, 'voltagent.tool.name') ??
    attrString(attrs, 'tool.name');
  const costUsd = computeCost(model, tokensIn, tokensOut);

  // Detect Claude / Gemini extended-thinking spans. VoltAgent surfaces
  // reasoning text via the same Vercel-AI key Mastra uses
  // (`ai.response.reasoning`); other pipelines use the OTel GenAI
  // semconv key. Accept all three so the dashboard's brain-emoji
  // subtype + Reasoning panel light up regardless.
  const thinkingText =
    attrString(attrs, 'ai.response.reasoning') ??
    attrString(attrs, 'gen_ai.response.thinking') ??
    attrString(attrs, 'anthropic.thinking') ??
    null;
  const isThinking = thinkingText !== null && thinkingText.length > 0;
  const spanSubtype: 'thinking' | null = isThinking ? 'thinking' : null;
  const thinkingTokens = isThinking
    ? Math.max(1, Math.floor(thinkingText!.length / 4))
    : null;

  // Input / output content. VoltAgent uses BARE `input` / `output`
  // attribute keys on the root + LLM + tool spans (verified against
  // node_modules/@voltagent/core/dist source). They sit at the END
  // of the priority list so namespaced keys (Vercel-AI / GenAI /
  // OpenClaw conventions) win when both are present — only when no
  // namespaced version exists do we fall back to the bare keys.
  const input =
    attrs['ai.prompt.messages'] ??
    attrs['ai.toolCall.args'] ??
    attrs['gen_ai.input.messages'] ??
    attrs['gen_ai.prompt'] ??
    attrs['voltagent.input'] ??
    attrs['agent.messages'] ??
    // Guardrail spans surface their checked content under guardrail.input.*
    attrs['guardrail.input.original'] ??
    attrs['guardrail.input.after'] ??
    attrs['input'] ??
    null;
  const output =
    attrs['ai.response.text'] ??
    attrs['ai.response.object'] ??
    attrs['ai.toolCall.result'] ??
    attrs['gen_ai.output.messages'] ??
    attrs['gen_ai.completion'] ??
    attrs['voltagent.output'] ??
    attrs['agent.step.text'] ??
    // Guardrail decisions land as output: the action + message describe
    // why a span was blocked / allowed.
    attrs['guardrail.message'] ??
    attrs['guardrail.output.after'] ??
    attrs['output'] ??
    null;

  // Lumin native error_message is a string. OTel tracks status code
  // separately from any exception event. Prefer a recorded exception
  // message; fall back to status description.
  let errorMessage: string | null = null;
  if (span.status?.code === SpanStatusCode.ERROR) {
    errorMessage = span.status.message ?? null;
  }
  for (const ev of span.events ?? []) {
    if (ev.name === 'exception') {
      const evAttrs = (ev.attributes ?? {}) as Record<string, unknown>;
      const msg = attrString(evAttrs, 'exception.message');
      if (msg) errorMessage = msg;
    }
  }

  // Session/user identifiers — span attrs first, then resource attrs
  // (which all spans in a process share). Resource fallback propagates
  // a TracerProvider-level session id to every child without each
  // span needing to set it itself.
  const sessionId =
    attrString(attrs, 'session.id') ??
    attrString(attrs, 'gen_ai.conversation.id') ??
    attrString(attrs, 'voltagent.session_id') ??
    attrString(attrs, 'agent.parent.id') ??
    attrString(resourceAttrs, 'session.id') ??
    attrString(resourceAttrs, 'voltagent.session_id') ??
    null;

  // OTel SDK has moved from `parentSpanId` to `parentSpanContext.spanId`
  // across versions. Read whichever is present.
  const parentFromCtx = (
    span as unknown as { parentSpanContext?: { spanId?: string } }
  ).parentSpanContext?.spanId;
  const parentLegacy = (span as unknown as { parentSpanId?: string })
    .parentSpanId;
  const parentSpanId = parentFromCtx ?? parentLegacy ?? null;

  // For thinking spans, prefer the reasoning text as `input` (so the
  // dashboard's "Reasoning" panel renders it) and clear `output`.
  const finalInput = isThinking
    ? serialize({ thinking: thinkingText }, maxPayloadSize)
    : serialize(input, maxPayloadSize);
  const finalOutput = isThinking
    ? null
    : serialize(output, maxPayloadSize);

  return {
    id: ctx.spanId,
    trace_id: ctx.traceId,
    parent_span_id: parentSpanId,
    name: isThinking ? 'thinking' : span.name,
    type: isThinking ? 'llm' : luminType,
    input: finalInput,
    output: finalOutput,
    started_at: hrTimeToIso(span.startTime) ?? new Date().toISOString(),
    ended_at: hrTimeToIso(span.endTime),
    error: errorMessage,
    session_id: sessionId,
    span_subtype: spanSubtype,
    thinking_tokens: thinkingTokens,
    model,
    provider,
    tokens_input: tokensIn,
    tokens_output: tokensOut,
    cost_usd: costUsd,
    tool_name: toolName,
    metadata: attrs as Record<string, unknown>,
  };
}

/**
 * OTel SpanExporter that ships VoltAgent agent spans to a running
 * Lumin instance. Drop-in for any OTel pipeline.
 */
export class LuminExporter implements SpanExporter {
  private readonly host: string;
  private readonly apiKey: string | undefined;
  private readonly project: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly maxPayloadSize: number;
  private shutdownCalled = false;

  constructor(config: LuminExporterConfig = {}) {
    this.host = (
      config.host ??
      (typeof process !== 'undefined' ? process.env.LUMIN_HOST : undefined) ??
      DEFAULT_HOST
    ).replace(/\/+$/, '');
    this.apiKey =
      config.apiKey ??
      (typeof process !== 'undefined'
        ? process.env.LUMIN_API_KEY
        : undefined);
    this.project = config.project ?? DEFAULT_PROJECT;
    this.timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = config.fetchImpl ?? fetch;
    this.maxPayloadSize = config.maxPayloadSize ?? DEFAULT_MAX_PAYLOAD;
  }

  export(
    spans: ReadableSpan[],
    resultCallback: (result: ExportResult) => void,
  ): void {
    if (this.shutdownCalled || spans.length === 0) {
      resultCallback({ code: ExportResultCode.SUCCESS });
      return;
    }
    void this.send(spans).then(
      () => resultCallback({ code: ExportResultCode.SUCCESS }),
      // Lumin Rule 7: never let a network failure surface to the
      // user. Report SUCCESS even on failure so OTel SDK does not
      // retry indefinitely or throw at the caller.
      () => resultCallback({ code: ExportResultCode.SUCCESS }),
    );
  }

  async shutdown(): Promise<void> {
    this.shutdownCalled = true;
  }

  async forceFlush(): Promise<void> {}

  private async send(otelSpans: ReadableSpan[]): Promise<void> {
    const luminSpans = otelSpans.map((s) =>
      otelSpanToLumin(s, this.maxPayloadSize),
    );

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-Lumin-Project': this.project,
    };
    if (this.apiKey) {
      headers['Authorization'] = `Bearer ${this.apiKey}`;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const resp = await this.fetchImpl(`${this.host}/v1/spans`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ spans: luminSpans }),
        signal: controller.signal,
      });
      try {
        await resp.text();
      } catch {
        /* swallow */
      }
    } finally {
      clearTimeout(timer);
    }
  }
}
