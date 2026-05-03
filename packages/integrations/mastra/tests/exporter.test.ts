import { describe, expect, test, vi, beforeEach } from 'vitest';
import { ExportResultCode } from '@opentelemetry/core';
import { SpanKind, SpanStatusCode } from '@opentelemetry/api';
import type { ReadableSpan } from '@opentelemetry/sdk-trace-base';
import {
  SynapticExporter,
  otelSpanToSynaptic,
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

describe('otelSpanToSynaptic', () => {
  test('basic fields map across', () => {
    const span = makeSpan({
      name: 'my_agent.run',
      attributes: { 'mastra.input': 'hello' },
    });
    const out = otelSpanToSynaptic(span);
    expect(out.id).toBe('bbbbbbbbbbbbbbbb');
    expect(out.trace_id).toBe('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    expect(out.name).toBe('my_agent.run');
    expect(out.parent_span_id).toBeNull();
    expect(out.started_at).toBe('2023-11-14T22:13:20.000Z');
    expect(out.ended_at).toBe('2023-11-14T22:13:21.500Z');
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
    const out = otelSpanToSynaptic(span);
    expect(out.type).toBe('llm');
    expect(out.provider).toBe('openai');
    // response.model wins over request.model
    expect(out.model).toBe('gpt-4o-2024-11-20');
    expect(out.tokens_input).toBe(234);
    expect(out.tokens_output).toBe(18);
  });

  test('tool span: gen_ai.tool.name yields type=tool', () => {
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: {
        'gen_ai.tool.name': 'search_web',
        'gen_ai.tool.type': 'function',
      },
    });
    const out = otelSpanToSynaptic(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('search_web');
  });

  test('SpanKind.INTERNAL with no GenAI attrs → custom', () => {
    const span = makeSpan({ kind: SpanKind.INTERNAL });
    expect(otelSpanToSynaptic(span).type).toBe('custom');
  });

  test('parent_span_id propagates', () => {
    const span = makeSpan({ parentSpanId: 'cccccccccccccccc' });
    expect(otelSpanToSynaptic(span).parent_span_id).toBe('cccccccccccccccc');
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
    expect(otelSpanToSynaptic(span).error).toBe('rate limit exceeded');
  });

  test('OTel ERROR status with no exception event → status.message', () => {
    const span = makeSpan({
      status: { code: SpanStatusCode.ERROR, message: 'connection refused' },
    });
    expect(otelSpanToSynaptic(span).error).toBe('connection refused');
  });

  test('OK status → error is null', () => {
    const span = makeSpan({ status: { code: SpanStatusCode.OK } });
    expect(otelSpanToSynaptic(span).error).toBeNull();
  });

  test('mastra-style input/output captured', () => {
    const span = makeSpan({
      attributes: {
        'mastra.input': { question: 'What is 2+2?' },
        'mastra.output': { answer: '4' },
      },
    });
    const out = otelSpanToSynaptic(span);
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
    const out = otelSpanToSynaptic(span);
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
    const out = otelSpanToSynaptic(span);
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
    const out = otelSpanToSynaptic(span);
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
    const out = otelSpanToSynaptic(span);
    expect(out.input).toBe('system: be helpful\nuser: hello');
  });

  test('numeric and boolean attributes get stringified, not dropped', () => {
    const span = makeSpan({
      attributes: { 'mastra.input': 42 },
    });
    const out = otelSpanToSynaptic(span);
    expect(out.input).toBe('42');
  });

  test('session_id resolves from common conventions', () => {
    const a = otelSpanToSynaptic(
      makeSpan({ attributes: { 'session.id': 's-123' } }),
    );
    expect(a.session_id).toBe('s-123');

    const b = otelSpanToSynaptic(
      makeSpan({ attributes: { 'gen_ai.conversation.id': 's-456' } }),
    );
    expect(b.session_id).toBe('s-456');

    const c = otelSpanToSynaptic(
      makeSpan({ attributes: { 'mastra.session_id': 's-789' } }),
    );
    expect(c.session_id).toBe('s-789');
  });

  test('large input is truncated to maxPayloadSize', () => {
    const huge = 'x'.repeat(50_000);
    const span = makeSpan({ attributes: { 'mastra.input': huge } });
    const out = otelSpanToSynaptic(span, 1024);
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
    const out = otelSpanToSynaptic(span);
    // Bad-typed model is rejected by attrString — model stays null
    expect(out.model).toBeNull();
    // Bad-typed tokens are rejected too
    expect(out.tokens_input).toBeNull();
  });
});

// ---------- exporter ----------

describe('SynapticExporter', () => {
  let originalHost: string | undefined;
  beforeEach(() => {
    originalHost = process.env.SYNAPTIC_HOST;
  });

  test('export() POSTs to /v1/spans with the correct shape', async () => {
    const captured: { url: string; init?: RequestInit } = { url: '' };
    const fakeFetch: typeof fetch = async (url, init) => {
      captured.url = String(url);
      captured.init = init;
      return new Response('{"accepted":1}', { status: 200 });
    };

    const exporter = new SynapticExporter({
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
    expect(headers['X-Synaptic-Project']).toBe('mastra-test');
  });

  test('Authorization header set when apiKey provided', async () => {
    const captured: { headers?: Record<string, string> } = {};
    const fakeFetch: typeof fetch = async (_url, init) => {
      captured.headers = init?.headers as Record<string, string>;
      return new Response('{}');
    };
    const exporter = new SynapticExporter({
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
    const exporter = new SynapticExporter({
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
    const exporter = new SynapticExporter({
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
    const exporter = new SynapticExporter({
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
    const exporter = new SynapticExporter({
      host: 'http://x',
      fetchImpl: fakeFetch,
    });
    await exporter.shutdown();
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(posted).toBe(0);
  });

  test('SYNAPTIC_HOST env var is the default when host not given', async () => {
    process.env.SYNAPTIC_HOST = 'http://envhost:7777';
    const captured: { url: string } = { url: '' };
    const fakeFetch: typeof fetch = async (url) => {
      captured.url = String(url);
      return new Response('');
    };
    const exporter = new SynapticExporter({ fetchImpl: fakeFetch });
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(captured.url).toBe('http://envhost:7777/v1/spans');
  });

  test('host with trailing slash is normalized', async () => {
    const captured: { url: string } = { url: '' };
    const fakeFetch: typeof fetch = async (url) => {
      captured.url = String(url);
      return new Response('');
    };
    const exporter = new SynapticExporter({
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
    const exporter = new SynapticExporter({
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
    if (originalHost === undefined) delete process.env.SYNAPTIC_HOST;
    else process.env.SYNAPTIC_HOST = originalHost;
    expect(true).toBe(true);
  });
});
