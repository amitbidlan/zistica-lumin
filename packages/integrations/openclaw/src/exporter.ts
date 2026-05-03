/**
 * Synaptic exporter for OpenClaw and any other OTel-based agent framework.
 *
 * OpenClaw ships OTel telemetry through `@openclaw/diagnostics-otel`.
 * This exporter plugs into that pipeline and converts each batch of
 * OTel spans into Synaptic's native JSON span format, POSTing to
 * `{host}/v1/spans`.
 *
 * Why not OTLP protobuf? Synaptic's API exposes `/v1/spans` (native
 * JSON) as the documented ingest path. Routing through native JSON
 * keeps this package self-contained: no protobuf runtime, no schema
 * codegen — minimal install footprint.
 *
 * Resilience (Synaptic Rule 7): the agent must never fail because
 * Synaptic is unreachable. All network errors are swallowed; export()
 * always reports success to the OTel pipeline so the SDK doesn't
 * retry indefinitely or surface errors to user code.
 */

import type {
  ReadableSpan,
  SpanExporter,
} from '@opentelemetry/sdk-trace-base';
import { ExportResultCode, type ExportResult } from '@opentelemetry/core';
import { SpanKind, SpanStatusCode } from '@opentelemetry/api';

export interface SynapticExporterConfig {
  /** Synaptic API base URL. Defaults to env SYNAPTIC_HOST or http://localhost:8000. */
  host?: string;
  /** Optional API key for hosted Synaptic (sent as Authorization: Bearer). */
  apiKey?: string;
  /** Project tag for grouping. Defaults to "openclaw". */
  project?: string;
  /** Per-export network timeout in milliseconds. Default 5_000. */
  timeoutMs?: number;
  /** Inject a custom fetch impl — useful for tests. */
  fetchImpl?: typeof fetch;
  /** Maximum size for serialized input/output payloads. Default 10_240. */
  maxPayloadSize?: number;
}

interface SynapticSpan {
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
const DEFAULT_PROJECT = 'openclaw';
const DEFAULT_MAX_PAYLOAD = 10_240;

/**
 * Per-1k-token USD prices for cost estimation. Same shape as the
 * tables in the LangChain / LlamaIndex / Anthropic Python integrations
 * so cost numbers are consistent across frameworks. Longest-prefix
 * match against the (lowercased) model name.
 *
 * Real production model names often look like
 * `ft:gpt-4o:my-org::abc123` or `openai/gpt-4o-mini` — `normalizeModel`
 * strips those prefixes before matching.
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

/**
 * Public hook to register a custom price for self-hosted or
 * not-yet-supported models. Same algorithm as the LangChain /
 * LlamaIndex integrations.
 */
export function registerModelPrice(
  modelPrefix: string,
  inputPer1k: number,
  outputPer1k: number,
): void {
  PRICES_PER_1K[modelPrefix.toLowerCase()] = [inputPer1k, outputPer1k];
}

/**
 * Convert an OTel attribute value to a string suitable for the
 * Synaptic `input` / `output` field. OTel only delivers primitives
 * or arrays of primitives, so the cases are:
 *   - string: pass through (already JSON in most OpenClaw / Vercel
 *     AI conventions — re-stringifying would double-encode)
 *   - array of strings: join with newlines (each prompt message is
 *     its own array element)
 *   - other primitives: String(value)
 *   - object (rare; only if the SDK ever surfaces them): JSON.stringify
 *
 * Always capped at maxSize.
 */
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

/**
 * OTel `[seconds, nanos]` HrTime → ISO-8601 string in UTC with
 * microsecond precision (ISO8601 allows up to 9 fractional digits).
 *
 * `new Date(ms).toISOString()` only preserves millisecond precision
 * — adjacent rapid spans that straddle a ms boundary lose their
 * ordering. Construct the ISO string directly from the hr_time
 * tuple so children that end within microseconds of a parent stay
 * temporally nested.
 */
function hrTimeToIso(time: [number, number] | undefined | null): string | null {
  if (!time) return null;
  const [seconds, nanos] = time;
  // Build the YYYY-MM-DDTHH:MM:SS via Date (this part is ms-safe),
  // then append a 6-digit microsecond fragment computed from the
  // raw nano portion — preserves sub-ms ordering.
  const ms = seconds * 1000 + Math.floor(nanos / 1_000_000);
  const micros = Math.floor(nanos / 1_000) % 1_000_000;
  const isoMs = new Date(ms).toISOString(); // ends in ".###Z"
  // Replace the ".###Z" suffix with ".######Z" preserving micros
  return isoMs.slice(0, -5) + '.' + String(micros).padStart(6, '0') + 'Z';
}

/**
 * Map an OTel `SpanKind` and the GenAI attributes to a Synaptic
 * span `type`. The mapping mirrors how the Synaptic LangChain /
 * CrewAI / Mastra integrations classify spans elsewhere — keeps
 * the dashboard rendering consistent across frameworks.
 */
function classifySpanType(span: ReadableSpan): string {
  const attrs = span.attributes ?? {};
  if (attrs['gen_ai.operation.name'] || attrs['gen_ai.request.model']) {
    return 'llm';
  }
  if (attrs['gen_ai.tool.name'] || attrs['gen_ai.tool.type']) {
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

/** Convert one OTel ReadableSpan to a Synaptic SpanInput. */
export function otelSpanToSynaptic(
  span: ReadableSpan,
  maxPayloadSize: number = DEFAULT_MAX_PAYLOAD,
): SynapticSpan {
  const ctx = span.spanContext();
  const attrs = (span.attributes ?? {}) as Record<string, unknown>;
  const resourceAttrs = ((span.resource as { attributes?: Record<string, unknown> })?.attributes ?? {}) as Record<string, unknown>;

  const synapticType = classifySpanType(span);
  const model =
    attrString(attrs, 'gen_ai.response.model') ??
    attrString(attrs, 'gen_ai.request.model');
  const provider = attrString(attrs, 'gen_ai.system');
  const tokensIn = attrNumber(attrs, 'gen_ai.usage.input_tokens');
  const tokensOut = attrNumber(attrs, 'gen_ai.usage.output_tokens');
  const toolName = attrString(attrs, 'gen_ai.tool.name');
  const costUsd = computeCost(model, tokensIn, tokensOut);

  // Detect Claude extended-thinking spans. OpenClaw / Anthropic
  // surface reasoning text via `gen_ai.response.thinking` (Anthropic
  // SDK semantic convention) or via blocks inside the message
  // content. Stamp `span_subtype="thinking"` so the dashboard
  // renders the brain-emoji thinking row.
  const thinkingText =
    attrString(attrs, 'gen_ai.response.thinking') ??
    attrString(attrs, 'anthropic.thinking') ??
    null;
  const isThinking = thinkingText !== null && thinkingText.length > 0;
  const spanSubtype: 'thinking' | null = isThinking ? 'thinking' : null;
  const thinkingTokens = isThinking
    ? Math.max(1, Math.floor(thinkingText!.length / 4))
    : null;

  // OpenClaw / Vercel AI / OTel conventions all stash prompt + response
  // text on different attribute keys depending on which provider is
  // in play. Read them all, in priority order.
  const input =
    attrs['ai.prompt.messages'] ??
    attrs['ai.toolCall.args'] ??
    attrs['gen_ai.input.messages'] ??
    attrs['gen_ai.prompt'] ??
    attrs['openclaw.input'] ??
    null;
  const output =
    attrs['ai.response.text'] ??
    attrs['ai.response.object'] ??
    attrs['ai.toolCall.result'] ??
    attrs['gen_ai.output.messages'] ??
    attrs['gen_ai.completion'] ??
    attrs['openclaw.output'] ??
    null;

  // Synaptic native error_message is a string. OTel tracks status code
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

  // Session/user identifiers — accepted from common conventions.
  // OpenClaw exposes a per-conversation session id via the
  // diagnostics layer. Look on the span's own attrs, then fall
  // back to the *resource* attributes (which all spans in a
  // process share). This propagates the channel/session id from
  // the Mastra-style root span down to every child without each
  // span needing to set the attribute itself.
  const sessionId =
    attrString(attrs, 'session.id') ??
    attrString(attrs, 'gen_ai.conversation.id') ??
    attrString(attrs, 'openclaw.session_id') ??
    attrString(attrs, 'openclaw.channel.id') ??
    attrString(resourceAttrs, 'session.id') ??
    attrString(resourceAttrs, 'openclaw.session_id') ??
    attrString(resourceAttrs, 'openclaw.channel.id') ??
    null;

  // OpenClaw delivers messages over channels (whatsapp, telegram,
  // discord, slack, …). Surface the channel name as a metadata-only
  // hint by leaving it in attrs (already on the span); we don't
  // promote it to a top-level field since the Synaptic span shape
  // doesn't have one.

  // OTel SDK has moved from `parentSpanId` to `parentSpanContext.spanId`
  // across versions. Read whichever is present.
  const parentFromCtx = (
    span as unknown as { parentSpanContext?: { spanId?: string } }
  ).parentSpanContext?.spanId;
  const parentLegacy = (span as unknown as { parentSpanId?: string })
    .parentSpanId;
  const parentSpanId = parentFromCtx ?? parentLegacy ?? null;

  // For thinking spans, prefer the reasoning text as `input` (so
  // the dashboard's "Reasoning" panel renders it) and clear the
  // confusingly-similar `output` field.
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
    type: isThinking ? 'llm' : synapticType,
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
 * OTel SpanExporter that ships batches of OpenClaw agent spans to a
 * running Synaptic instance. Drop-in for any OTel pipeline; designed
 * to plug into `@openclaw/diagnostics-otel`'s exporter slot.
 */
export class SynapticExporter implements SpanExporter {
  private readonly host: string;
  private readonly apiKey: string | undefined;
  private readonly project: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly maxPayloadSize: number;
  private shutdownCalled = false;

  constructor(config: SynapticExporterConfig = {}) {
    this.host = (
      config.host ??
      (typeof process !== 'undefined' ? process.env.SYNAPTIC_HOST : undefined) ??
      DEFAULT_HOST
    ).replace(/\/+$/, '');
    this.apiKey =
      config.apiKey ??
      (typeof process !== 'undefined'
        ? process.env.SYNAPTIC_API_KEY
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
      // Synaptic Rule 7: never let a network failure surface to the
      // user. Report SUCCESS even on failure so OTel SDK does not
      // retry indefinitely or throw at the caller.
      () => resultCallback({ code: ExportResultCode.SUCCESS }),
    );
  }

  async shutdown(): Promise<void> {
    this.shutdownCalled = true;
  }

  async forceFlush(): Promise<void> {
    // Nothing buffered locally — OTel BatchSpanProcessor handles batching.
  }

  /** Serialize and POST the batch. Internal — caller wraps errors. */
  private async send(otelSpans: ReadableSpan[]): Promise<void> {
    const synapticSpans = otelSpans.map((s) =>
      otelSpanToSynaptic(s, this.maxPayloadSize),
    );

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-Synaptic-Project': this.project,
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
        body: JSON.stringify({ spans: synapticSpans }),
        signal: controller.signal,
      });
      // Drain body to free socket; ignore status code per Rule 7
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
