import { randomUUID } from 'node:crypto';

export interface SpanData {
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
  /** Optional session grouping. Populated from the current session() context
   *  or via trace(fn, { sessionId }). Default null preserves prior behavior. */
  session_id: string | null;
  /** Optional discriminator for spans that originate from a typed
   *  integration. Currently used for Claude extended-thinking — the
   *  integration emits `claude_call` parents with `thinking` and
   *  `response` children. Default null = ordinary span. */
  span_subtype?: 'thinking' | 'response' | string | null;
  /** Estimated tokens spent inside a thinking block. Only set on
   *  thinking spans. Anthropic doesn't break thinking out of
   *  output_tokens — we estimate from text length. */
  thinking_tokens?: number | null;
  /** LLM model identifier (e.g. "claude-opus-4-20250514"). */
  model?: string | null;
  provider?: string | null;
  tokens_input?: number | null;
  tokens_output?: number | null;
  cost_usd?: number | null;
}

function serialize(value: unknown, maxSize: number): string | null {
  if (value === null || value === undefined) return null;
  let s: string;
  try {
    s = JSON.stringify(value);
    if (s === undefined) s = String(value);
  } catch {
    try {
      s = String(value);
    } catch {
      s = '<unserializable>';
    }
  }
  return s.length > maxSize ? s.slice(0, maxSize) : s;
}

export class Span {
  readonly id: string;
  readonly trace_id: string;
  readonly parent_span_id: string | null;
  readonly name: string;
  readonly type: string;
  readonly started_at: string;
  input: string | null = null;
  output: string | null = null;
  ended_at: string | null = null;
  error: string | null = null;
  session_id: string | null = null;
  span_subtype: 'thinking' | 'response' | string | null = null;
  thinking_tokens: number | null = null;
  model: string | null = null;
  provider: string | null = null;
  tokens_input: number | null = null;
  tokens_output: number | null = null;
  cost_usd: number | null = null;

  private constructor(
    id: string,
    trace_id: string,
    parent_span_id: string | null,
    name: string,
    type: string,
  ) {
    this.id = id;
    this.trace_id = trace_id;
    this.parent_span_id = parent_span_id;
    this.name = name;
    this.type = type;
    this.started_at = new Date().toISOString();
  }

  static create(name: string, type = 'custom', parent?: Span | null): Span {
    const id = randomUUID();
    if (parent) {
      return new Span(id, parent.trace_id, parent.id, name, type);
    }
    return new Span(id, id, null, name, type);
  }

  setInput(value: unknown, maxSize = 10_240): void {
    this.input = serialize(value, maxSize);
  }

  setOutput(value: unknown, maxSize = 10_240): void {
    this.output = serialize(value, maxSize);
  }

  setError(err: unknown): void {
    if (err instanceof Error) {
      this.error = `${err.constructor.name}: ${err.message}`;
    } else {
      this.error = String(err);
    }
  }

  end(): void {
    if (this.ended_at === null) {
      this.ended_at = new Date().toISOString();
    }
  }

  toJSON(): SpanData {
    return {
      id: this.id,
      trace_id: this.trace_id,
      parent_span_id: this.parent_span_id,
      name: this.name,
      type: this.type,
      input: this.input,
      output: this.output,
      started_at: this.started_at,
      ended_at: this.ended_at,
      error: this.error,
      session_id: this.session_id,
      span_subtype: this.span_subtype,
      thinking_tokens: this.thinking_tokens,
      model: this.model,
      provider: this.provider,
      tokens_input: this.tokens_input,
      tokens_output: this.tokens_output,
      cost_usd: this.cost_usd,
    };
  }
}
