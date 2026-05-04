import { describe, expect, test, vi, beforeEach } from 'vitest';
import { ExportResultCode } from '@opentelemetry/core';
import { SpanKind, SpanStatusCode } from '@opentelemetry/api';
import type { ReadableSpan } from '@opentelemetry/sdk-trace-base';
import {
  LuminExporter,
  otelSpanToLumin,
  registerModelPrice,
} from '../src/exporter.js';

/** Build a ReadableSpan-shaped object good enough for the mapper. */
function makeSpan(overrides: Partial<ReadableSpan> & {
  attributes?: Record<string, unknown>;
} = {}): ReadableSpan {
  const base: ReadableSpan = {
    name: 'test_span',
    kind: SpanKind.CLIENT,
    spanContext: () => ({
      traceId: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      spanId: 'bbbbbbbbbbbbbbbb',
      traceFlags: 0,
      isRemote: false,
    }),
    parentSpanId: undefined,
    startTime: [1700000000, 0],
    endTime: [1700000001, 500_000_000],
    status: { code: SpanStatusCode.UNSET },
    attributes: {},
    links: [],
    events: [],
    duration: [1, 500_000_000],
    ended: true,
    resource: {
      attributes: {},
      merge: () => base.resource,
    } as ReadableSpan['resource'],
    instrumentationLibrary: { name: 'test', version: '0.0.0' },
    droppedAttributesCount: 0,
    droppedEventsCount: 0,
    droppedLinksCount: 0,
    ...overrides,
  };
  return base;
}

// ---------- mapper ----------

describe('otelSpanToLumin', () => {
  test('basic fields map across', () => {
    const span = makeSpan({
      name: 'my_agent.run',
      attributes: { 'mastra.input': 'hello' },
    });
    const out = otelSpanToLumin(span);
    expect(out.id).toBe('bbbbbbbbbbbbbbbb');
    expect(out.trace_id).toBe('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    expect(out.name).toBe('my_agent.run');
    expect(out.parent_span_id).toBeNull();
    // Microsecond-precision ISO format preserves sub-ms ordering
    // (matches OpenClaw mapper). Old format ".000Z" is gone — the
    // mapper now always returns ".000000Z".
    expect(out.started_at).toBe('2023-11-14T22:13:20.000000Z');
    expect(out.ended_at).toBe('2023-11-14T22:13:21.500000Z');
  });

  test('GenAI attributes populate model/provider/tokens', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.system': 'openai',
        'gen_ai.request.model': 'gpt-4o',
        'gen_ai.response.model': 'gpt-4o-2024-11-20',
        'gen_ai.usage.input_tokens': 234,
        'gen_ai.usage.output_tokens': 18,
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('llm');
    expect(out.provider).toBe('openai');
    // response.model wins over request.model
    expect(out.model).toBe('gpt-4o-2024-11-20');
    expect(out.tokens_input).toBe(234);
    expect(out.tokens_output).toBe(18);
  });

  test('Mastra ai.model.id / ai.model.provider used as fallback', () => {
    // Real @mastra/core emits `ai.model.id` and `ai.model.provider`
    // in addition to (and on some span types instead of) the OTel
    // GenAI semconv keys. Verified against the actual upstream.
    const span = makeSpan({
      attributes: {
        'ai.model.id': 'gpt-4o-mini',
        'ai.model.provider': 'openai',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.model).toBe('gpt-4o-mini');
    expect(out.provider).toBe('openai');
  });

  test('legacy gen_ai.usage.{prompt,completion}_tokens are accepted', () => {
    // Older Mastra builds + earlier Vercel AI SDK versions still
    // emit the prompt_tokens / completion_tokens names from the
    // pre-2025 GenAI semconv revision.
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'gpt-4o',
        'gen_ai.usage.prompt_tokens': 1000,
        'gen_ai.usage.completion_tokens': 200,
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.tokens_input).toBe(1000);
    expect(out.tokens_output).toBe(200);
    // And cost still computes correctly via the legacy names
    expect(out.cost_usd).toBeCloseTo(0.0045, 6);
  });

  test('tool span: gen_ai.tool.name yields type=tool', () => {
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: {
        'gen_ai.tool.name': 'search_web',
        'gen_ai.tool.type': 'function',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('search_web');
  });

  test('tool span: bare tool.name (Vercel/Mastra style) yields type=tool', () => {
    // The Vercel AI SDK and Mastra in-the-wild emit `tool.name`
    // without the `gen_ai.` prefix. Real demos hit this path far
    // more often than the formal semconv key.
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: { 'tool.name': 'pinecone_query' },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('pinecone_query');
  });

  test('tool span: mastra.tool.name namespace also recognized', () => {
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: { 'mastra.tool.name': 'shopify_get_order' },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('shopify_get_order');
  });

  test('SpanKind.INTERNAL with no GenAI attrs → custom', () => {
    const span = makeSpan({ kind: SpanKind.INTERNAL });
    expect(otelSpanToLumin(span).type).toBe('custom');
  });

  test('parent_span_id propagates', () => {
    const span = makeSpan({ parentSpanId: 'cccccccccccccccc' });
    expect(otelSpanToLumin(span).parent_span_id).toBe('cccccccccccccccc');
  });

  test('exception event → error_message', () => {
    const span = makeSpan({
      status: { code: SpanStatusCode.ERROR, message: 'fallback' },
      events: [
        {
          name: 'exception',
          time: [1700000000, 0],
          attributes: {
            'exception.message': 'rate limit exceeded',
            'exception.type': 'RateLimitError',
          },
        },
      ],
    });
    expect(otelSpanToLumin(span).error).toBe('rate limit exceeded');
  });

  test('OTel ERROR status with no exception event → status.message', () => {
    const span = makeSpan({
      status: { code: SpanStatusCode.ERROR, message: 'connection refused' },
    });
    expect(otelSpanToLumin(span).error).toBe('connection refused');
  });

  test('OK status → error is null', () => {
    const span = makeSpan({ status: { code: SpanStatusCode.OK } });
    expect(otelSpanToLumin(span).error).toBeNull();
  });

  test('mastra-style input/output captured', () => {
    const span = makeSpan({
      attributes: {
        'mastra.input': { question: 'What is 2+2?' },
        'mastra.output': { answer: '4' },
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toContain('2+2');
    expect(out.output).toContain('"4"');
  });

  test('legacy gen_ai.prompt / gen_ai.completion still recognized', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.prompt': 'p',
        'gen_ai.completion': 'c',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toContain('p');
    expect(out.output).toContain('c');
  });

  test('Vercel AI SDK / Mastra ai.prompt.messages and ai.response.text', () => {
    // These are the keys Mastra actually emits in production. OTel
    // attribute constraints mean prompt.messages is JSON-encoded as
    // a string; response.text is a plain string. Don't double-encode.
    const promptJson = JSON.stringify([
      { role: 'user', content: 'What is the capital of France?' },
    ]);
    const span = makeSpan({
      attributes: {
        'ai.prompt.messages': promptJson,
        'ai.response.text': 'The capital of France is Paris.',
      },
    });
    const out = otelSpanToLumin(span);
    // The pre-stringified JSON must NOT be wrapped in extra quotes
    expect(out.input).toBe(promptJson);
    expect(out.output).toBe('The capital of France is Paris.');
  });

  test('Vercel AI tool call: ai.toolCall.args + .result', () => {
    const argsJson = JSON.stringify({ query: 'capital of France' });
    const resultJson = JSON.stringify({
      results: ['Paris is the capital.'],
    });
    const span = makeSpan({
      attributes: {
        'ai.toolCall.args': argsJson,
        'ai.toolCall.result': resultJson,
        'gen_ai.tool.name': 'search_web',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('search_web');
    expect(out.input).toBe(argsJson);
    expect(out.output).toBe(resultJson);
  });

  test('array-of-strings attribute (e.g. message list) joins with newlines', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.prompt': ['system: be helpful', 'user: hello'],
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toBe('system: be helpful\nuser: hello');
  });

  test('numeric and boolean attributes get stringified, not dropped', () => {
    const span = makeSpan({
      attributes: { 'mastra.input': 42 },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toBe('42');
  });

  test('session_id resolves from common conventions', () => {
    const a = otelSpanToLumin(
      makeSpan({ attributes: { 'session.id': 's-123' } }),
    );
    expect(a.session_id).toBe('s-123');

    const b = otelSpanToLumin(
      makeSpan({ attributes: { 'gen_ai.conversation.id': 's-456' } }),
    );
    expect(b.session_id).toBe('s-456');

    const c = otelSpanToLumin(
      makeSpan({ attributes: { 'mastra.session_id': 's-789' } }),
    );
    expect(c.session_id).toBe('s-789');
  });

  test('session_id falls back to the resource attributes', () => {
    // The TracerProvider-level resource is the natural place to put a
    // process- or workflow-wide session id; spans that don't set their
    // own should inherit it.
    const span = makeSpan({
      attributes: {},
      resource: {
        attributes: { 'session.id': 'resource-sess-001' },
        merge: () => null as never,
      } as ReadableSpan['resource'],
    });
    expect(otelSpanToLumin(span).session_id).toBe('resource-sess-001');
  });

  test('span-level session.id wins over resource', () => {
    const span = makeSpan({
      attributes: { 'session.id': 'span-sess' },
      resource: {
        attributes: { 'session.id': 'resource-sess' },
        merge: () => null as never,
      } as ReadableSpan['resource'],
    });
    expect(otelSpanToLumin(span).session_id).toBe('span-sess');
  });

  // ---------- cost computation ----------

  test('cost_usd is computed for known models', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.system': 'anthropic',
        'gen_ai.request.model': 'claude-sonnet-4',
        'gen_ai.response.model': 'claude-sonnet-4-20250514',
        'gen_ai.usage.input_tokens': 1000,
        'gen_ai.usage.output_tokens': 500,
      },
    });
    const out = otelSpanToLumin(span);
    // claude-sonnet-4: $0.003/1k in, $0.015/1k out
    // 1000*0.003/1000 + 500*0.015/1000 = 0.003 + 0.0075 = 0.0105
    expect(out.cost_usd).toBeCloseTo(0.0105, 6);
  });

  test('cost_usd is null when model has no price entry', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'mystery-llm-9000',
        'gen_ai.usage.input_tokens': 100,
        'gen_ai.usage.output_tokens': 50,
      },
    });
    expect(otelSpanToLumin(span).cost_usd).toBeNull();
  });

  test('cost_usd is null when token counts are absent', () => {
    const span = makeSpan({
      attributes: { 'gen_ai.request.model': 'gpt-4o' },
    });
    expect(otelSpanToLumin(span).cost_usd).toBeNull();
  });

  test('fine-tuned model (ft:gpt-4o:org::abc) maps to base price', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'ft:gpt-4o:my-org::abc123',
        'gen_ai.usage.input_tokens': 1000,
        'gen_ai.usage.output_tokens': 1000,
      },
    });
    const out = otelSpanToLumin(span);
    // gpt-4o: $0.0025/1k in, $0.010/1k out → 0.0025 + 0.010 = 0.0125
    expect(out.cost_usd).toBeCloseTo(0.0125, 6);
  });

  test('provider-prefixed model (openai/gpt-4o-mini) is normalized', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'openai/gpt-4o-mini',
        'gen_ai.usage.input_tokens': 1000,
        'gen_ai.usage.output_tokens': 1000,
      },
    });
    const out = otelSpanToLumin(span);
    // gpt-4o-mini: $0.00015/1k in, $0.0006/1k out
    expect(out.cost_usd).toBeCloseTo(0.00075, 8);
  });

  test('registerModelPrice teaches the table about a self-hosted model', () => {
    registerModelPrice('llama-3-70b-vendor-x', 0.0008, 0.0024);
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'llama-3-70b-vendor-x',
        'gen_ai.usage.input_tokens': 1000,
        'gen_ai.usage.output_tokens': 1000,
      },
    });
    expect(otelSpanToLumin(span).cost_usd).toBeCloseTo(0.0032, 6);
  });

  // ---------- Claude extended-thinking detection ----------

  test('thinking span: gen_ai.response.thinking sets span_subtype + name', () => {
    const span = makeSpan({
      name: 'agent.reason',
      attributes: {
        'gen_ai.system': 'anthropic',
        'gen_ai.request.model': 'claude-sonnet-4',
        'gen_ai.response.thinking':
          'Let me think step by step. The user wants X. The constraints are Y. Therefore Z.',
        'gen_ai.usage.input_tokens': 100,
        'gen_ai.usage.output_tokens': 200,
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.span_subtype).toBe('thinking');
    expect(out.name).toBe('thinking');
    expect(out.type).toBe('llm');
    // Reasoning text moves into `input` so the dashboard's Reasoning
    // panel renders it
    expect(out.input).toContain('think step by step');
    // And `output` is cleared (dashboard doesn't render duplicate text)
    expect(out.output).toBeNull();
    // thinking_tokens estimated from text length (chars/4)
    expect(out.thinking_tokens).toBeGreaterThan(0);
  });

  test('non-thinking LLM span keeps span_subtype null', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'gpt-4o',
        'ai.response.text': 'just a normal answer',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.span_subtype).toBeNull();
    expect(out.thinking_tokens).toBeNull();
  });

  test('anthropic.thinking attribute also recognized as thinking', () => {
    const span = makeSpan({
      attributes: {
        'anthropic.thinking': 'reasoning content',
        'gen_ai.request.model': 'claude-opus-4',
      },
    });
    expect(otelSpanToLumin(span).span_subtype).toBe('thinking');
  });

  test('real Mastra ai.response.reasoning is detected as thinking', () => {
    // Verified against @mastra/core: Mastra surfaces extended-thinking
    // text under `ai.response.reasoning` (Vercel AI SDK convention,
    // not the OTel GenAI semconv key). Without this fallback Mastra
    // thinking spans never get the brain-emoji subtype.
    const span = makeSpan({
      attributes: {
        'gen_ai.system': 'anthropic',
        'gen_ai.request.model': 'claude-sonnet-4',
        'ai.response.reasoning':
          'The user is asking about pricing tiers — let me think about which plan fits their team size before answering.',
        'gen_ai.usage.input_tokens': 200,
        'gen_ai.usage.output_tokens': 350,
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.span_subtype).toBe('thinking');
    expect(out.name).toBe('thinking');
    expect(out.input).toContain('pricing tiers');
    expect(out.output).toBeNull();
  });

  test('empty thinking string is NOT treated as thinking', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.response.thinking': '',
        'gen_ai.request.model': 'claude-sonnet-4',
      },
    });
    expect(otelSpanToLumin(span).span_subtype).toBeNull();
  });

  test('large input is truncated to maxPayloadSize', () => {
    const huge = 'x'.repeat(50_000);
    const span = makeSpan({ attributes: { 'mastra.input': huge } });
    const out = otelSpanToLumin(span, 1024);
    expect(out.input).not.toBeNull();
    expect(out.input!.length).toBeLessThanOrEqual(1024);
  });

  test('non-string non-number attribute values do not break the mapper', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 12345 as unknown as string,
        'gen_ai.usage.input_tokens': 'not-a-number' as unknown as number,
      },
    });
    const out = otelSpanToLumin(span);
    // Bad-typed model is rejected by attrString — model stays null
    expect(out.model).toBeNull();
    // Bad-typed tokens are rejected too
    expect(out.tokens_input).toBeNull();
  });
});

// ---------- exporter ----------

describe('LuminExporter', () => {
  let originalHost: string | undefined;
  beforeEach(() => {
    originalHost = process.env.LUMIN_HOST;
  });

  test('export() POSTs to /v1/spans with the correct shape', async () => {
    const captured: { url: string; init?: RequestInit } = { url: '' };
    const fakeFetch: typeof fetch = async (url, init) => {
      captured.url = String(url);
      captured.init = init;
      return new Response('{"accepted":1}', { status: 200 });
    };

    const exporter = new LuminExporter({
      host: 'http://localhost:9999',
      project: 'mastra-test',
      fetchImpl: fakeFetch,
    });

    const result = await new Promise((resolve) => {
      exporter.export([makeSpan()], resolve);
    });

    expect((result as { code: ExportResultCode }).code).toBe(
      ExportResultCode.SUCCESS,
    );
    expect(captured.url).toBe('http://localhost:9999/v1/spans');
    const body = JSON.parse(String(captured.init?.body));
    expect(body.spans).toHaveLength(1);
    expect(body.spans[0].name).toBe('test_span');
    const headers = captured.init?.headers as Record<string, string>;
    expect(headers['X-Lumin-Project']).toBe('mastra-test');
  });

  test('Authorization header set when apiKey provided', async () => {
    const captured: { headers?: Record<string, string> } = {};
    const fakeFetch: typeof fetch = async (_url, init) => {
      captured.headers = init?.headers as Record<string, string>;
      return new Response('{}');
    };
    const exporter = new LuminExporter({
      host: 'http://x',
      apiKey: 'secret-token',
      fetchImpl: fakeFetch,
    });
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(captured.headers!['Authorization']).toBe('Bearer secret-token');
  });

  test('network error is swallowed (Rule 7) — export still reports SUCCESS', async () => {
    const fakeFetch: typeof fetch = async () => {
      throw new Error('ECONNREFUSED');
    };
    const exporter = new LuminExporter({
      host: 'http://unreachable',
      fetchImpl: fakeFetch,
    });
    const result = await new Promise<{ code: ExportResultCode }>((r) => {
      exporter.export([makeSpan()], r as (v: unknown) => void);
    });
    expect(result.code).toBe(ExportResultCode.SUCCESS);
  });

  test('non-2xx response is swallowed too — agent never sees it', async () => {
    const fakeFetch: typeof fetch = async () =>
      new Response('boom', { status: 500 });
    const exporter = new LuminExporter({
      host: 'http://x',
      fetchImpl: fakeFetch,
    });
    const result = await new Promise<{ code: ExportResultCode }>((r) => {
      exporter.export([makeSpan()], r as (v: unknown) => void);
    });
    expect(result.code).toBe(ExportResultCode.SUCCESS);
  });

  test('empty span list short-circuits with SUCCESS', async () => {
    let called = false;
    const fakeFetch: typeof fetch = async () => {
      called = true;
      return new Response('');
    };
    const exporter = new LuminExporter({
      host: 'http://x',
      fetchImpl: fakeFetch,
    });
    const result = await new Promise<{ code: ExportResultCode }>((r) => {
      exporter.export([], r as (v: unknown) => void);
    });
    expect(result.code).toBe(ExportResultCode.SUCCESS);
    expect(called).toBe(false);
  });

  test('after shutdown(), exports become no-ops', async () => {
    let posted = 0;
    const fakeFetch: typeof fetch = async () => {
      posted += 1;
      return new Response('');
    };
    const exporter = new LuminExporter({
      host: 'http://x',
      fetchImpl: fakeFetch,
    });
    await exporter.shutdown();
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(posted).toBe(0);
  });

  test('LUMIN_HOST env var is the default when host not given', async () => {
    process.env.LUMIN_HOST = 'http://envhost:7777';
    const captured: { url: string } = { url: '' };
    const fakeFetch: typeof fetch = async (url) => {
      captured.url = String(url);
      return new Response('');
    };
    const exporter = new LuminExporter({ fetchImpl: fakeFetch });
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(captured.url).toBe('http://envhost:7777/v1/spans');
  });

  test('host with trailing slash is normalized', async () => {
    const captured: { url: string } = { url: '' };
    const fakeFetch: typeof fetch = async (url) => {
      captured.url = String(url);
      return new Response('');
    };
    const exporter = new LuminExporter({
      host: 'http://x:8000/',
      fetchImpl: fakeFetch,
    });
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(captured.url).toBe('http://x:8000/v1/spans');
  });

  test('hung server is aborted by timeout — no hang, agent unaffected', async () => {
    const fakeFetch: typeof fetch = (_url, init) =>
      new Promise((_resolve, reject) => {
        const sig = init?.signal as AbortSignal | undefined;
        sig?.addEventListener('abort', () =>
          reject(new Error('AbortError')),
        );
      });
    const exporter = new LuminExporter({
      host: 'http://hang',
      timeoutMs: 50,
      fetchImpl: fakeFetch,
    });
    const start = Date.now();
    const result = await new Promise<{ code: ExportResultCode }>((r) => {
      exporter.export([makeSpan()], r as (v: unknown) => void);
    });
    const elapsed = Date.now() - start;
    expect(result.code).toBe(ExportResultCode.SUCCESS);
    expect(elapsed).toBeLessThan(1500);
  });

  // restore env
  test.each([['cleanup']])('cleanup', () => {
    if (originalHost === undefined) delete process.env.LUMIN_HOST;
    else process.env.LUMIN_HOST = originalHost;
    expect(true).toBe(true);
  });
});
