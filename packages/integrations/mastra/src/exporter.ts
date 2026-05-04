/**
 * Lumin exporter for Mastra (and any other OTel-based agent framework).
 *
 * Implements the standard OTel `SpanExporter` interface so it plugs into
 * any pipeline that already speaks OpenTelemetry. Each batch of OTel
 * spans is mapped to Lumin's native JSON span format and POSTed to
 * `{host}/v1/spans` — the existing Lumin ingestion endpoint.
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
  /** Project tag for grouping. Defaults to "mastra". */
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
const DEFAULT_PROJECT = 'mastra';
const DEFAULT_MAX_PAYLOAD = 10_240;

/**
 * Convert an OTel attribute value to a string suitable for the
 * Lumin `input` / `output` field. OTel only delivers primitives
 * or arrays of primitives, so the cases are:
 *   - string: pass through (already JSON in most Mastra/Vercel
 *     conventions — re-stringifying would double-encode)
 *   - array of strings: join with newlines (each Vercel-style
 *     prompt message is its own array element)
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

/** OTel `[seconds, nanos]` HrTime → ISO-8601 string in UTC. */
function hrTimeToIso(time: [number, number] | undefined | null): string | null {
  if (!time) return null;
  const [seconds, nanos] = time;
  const millis = seconds * 1000 + Math.floor(nanos / 1_000_000);
  return new Date(millis).toISOString();
}

/**
 * Map an OTel `SpanKind` and the GenAI attributes to a Lumin
 * span `type`. The mapping mirrors how the LangChain/CrewAI
 * integrations classify spans elsewhere in the codebase.
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

/**
 * Pull a string value from OTel attribute dict. OTel `AttributeValue`
 * is a union — coerce to string only if it is one.
 */
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

  const luminType = classifySpanType(span);
  const model =
    attrString(attrs, 'gen_ai.response.model') ??
    attrString(attrs, 'gen_ai.request.model');
  const provider = attrString(attrs, 'gen_ai.system');
  const tokensIn = attrNumber(attrs, 'gen_ai.usage.input_tokens');
  const tokensOut = attrNumber(attrs, 'gen_ai.usage.output_tokens');
  const toolName = attrString(attrs, 'gen_ai.tool.name');

  // OTel attribute values must be primitives or arrays of primitives —
  // object values are silently dropped by the SDK. So we look for
  // attributes Mastra and the Vercel AI SDK actually emit: strings
  // (often JSON-encoded), arrays of strings, or simple scalars.
  //
  // Order of preference reflects how widely each key is used in the
  // Mastra/Vercel-AI ecosystem:
  //   ai.prompt.messages / ai.response.text  — Vercel AI SDK (Mastra)
  //   gen_ai.input.messages / .output.messages — newer OTel GenAI
  //   gen_ai.prompt / .completion              — older OTel GenAI
  //   ai.toolCall.args / .result               — Vercel AI tool calls
  //   mastra.input / mastra.output             — pre-stringified
  const input =
    attrs['ai.prompt.messages'] ??
    attrs['ai.toolCall.args'] ??
    attrs['gen_ai.input.messages'] ??
    attrs['gen_ai.prompt'] ??
    attrs['mastra.input'] ??
    null;
  const output =
    attrs['ai.response.text'] ??
    attrs['ai.response.object'] ??
    attrs['ai.toolCall.result'] ??
    attrs['gen_ai.output.messages'] ??
    attrs['gen_ai.completion'] ??
    attrs['mastra.output'] ??
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

  // Session/user identifiers — accepted from common conventions
  const sessionId =
    attrString(attrs, 'session.id') ??
    attrString(attrs, 'gen_ai.conversation.id') ??
    attrString(attrs, 'mastra.session_id') ??
    null;

  // OTel SDK has moved from `parentSpanId` to `parentSpanContext.spanId`
  // across versions. Read whichever is present.
  const parentFromCtx = (
    span as unknown as { parentSpanContext?: { spanId?: string } }
  ).parentSpanContext?.spanId;
  const parentLegacy = (span as unknown as { parentSpanId?: string })
    .parentSpanId;
  const parentSpanId = parentFromCtx ?? parentLegacy ?? null;

  return {
    id: ctx.spanId,
    trace_id: ctx.traceId,
    parent_span_id: parentSpanId,
    name: span.name,
    type: luminType,
    input: serialize(input, maxPayloadSize),
    output: serialize(output, maxPayloadSize),
    started_at: hrTimeToIso(span.startTime) ?? new Date().toISOString(),
    ended_at: hrTimeToIso(span.endTime),
    error: errorMessage,
    session_id: sessionId,
    model,
    provider,
    tokens_input: tokensIn,
    tokens_output: tokensOut,
    cost_usd: null,
    tool_name: toolName,
    metadata: attrs as Record<string, unknown>,
  };
}

/**
 * OTel SpanExporter that ships batches of agent spans to a running
 * Lumin instance. Drop-in for any OTel pipeline; designed
 * specifically for the Mastra `observability.configs[*].exporters`
 * array.
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

  async forceFlush(): Promise<void> {
    // Nothing buffered locally — OTel BatchSpanProcessor handles batching.
  }

  /** Serialize and POST the batch. Internal — caller wraps errors. */
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
