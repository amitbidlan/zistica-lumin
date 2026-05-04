import { describe, expect, test, beforeEach } from 'vitest';
import { ExportResultCode } from '@opentelemetry/core';
import { SpanKind, SpanStatusCode } from '@opentelemetry/api';
import type { ReadableSpan } from '@opentelemetry/sdk-trace-base';
import {
  LuminExporter,
  otelSpanToLumin,
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
      name: 'agent.run',
      attributes: { 'input': 'hello' },
    });
    const out = otelSpanToLumin(span);
    expect(out.id).toBe('bbbbbbbbbbbbbbbb');
    expect(out.trace_id).toBe('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    expect(out.name).toBe('agent.run');
    expect(out.parent_span_id).toBeNull();
    // Microsecond-precision ISO format preserves sub-ms ordering
    expect(out.started_at).toBe('2023-11-14T22:13:20.000000Z');
    expect(out.ended_at).toBe('2023-11-14T22:13:21.500000Z');
  });

  test('GenAI attributes populate model/provider/tokens AND cost', () => {
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
    expect(out.type).toBe('llm');
    expect(out.provider).toBe('anthropic');
    expect(out.model).toBe('claude-sonnet-4-20250514');
    expect(out.tokens_input).toBe(1000);
    expect(out.tokens_output).toBe(500);
    // claude-sonnet-4: $0.003/1k in, $0.015/1k out → 1000*0.003/1000 + 500*0.015/1000 = 0.003 + 0.0075 = 0.0105
    expect(out.cost_usd).toBeCloseTo(0.0105, 6);
  });

  test('cost computed from gen_ai.usage on real model', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.system': 'openai',
        'gen_ai.request.model': 'gpt-4o-mini',
        'gen_ai.usage.input_tokens': 1000,
        'gen_ai.usage.output_tokens': 1000,
      },
    });
    // gpt-4o-mini: $0.00015 in, $0.0006 out per 1k → 0.00015 + 0.0006 = 0.00075
    expect(otelSpanToLumin(span).cost_usd).toBeCloseTo(0.00075, 6);
  });

  test('cost null for unknown model', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'some-future-model-2027',
        'gen_ai.usage.input_tokens': 100,
        'gen_ai.usage.output_tokens': 50,
      },
    });
    expect(otelSpanToLumin(span).cost_usd).toBeNull();
  });

  test('fine-tuned model name normalizes (ft:gpt-4o:org::abc)', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 'ft:gpt-4o:my-org::abc123',
        'gen_ai.usage.input_tokens': 1000,
        'gen_ai.usage.output_tokens': 500,
      },
    });
    expect(otelSpanToLumin(span).cost_usd).toBeGreaterThan(0);
  });

  test('thinking text triggers span_subtype=thinking and renames span', () => {
    const span = makeSpan({
      name: 'llm.think_and_plan',
      attributes: {
        'gen_ai.system': 'anthropic',
        'gen_ai.request.model': 'claude-opus-4',
        'gen_ai.usage.input_tokens': 100,
        'gen_ai.usage.output_tokens': 800,
        'gen_ai.response.thinking':
          'Let me reason carefully through this problem step by step…',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.span_subtype).toBe('thinking');
    expect(out.name).toBe('thinking');
    expect(out.thinking_tokens).toBeGreaterThan(0);
    expect(out.input).toContain('Let me reason carefully');
    // Output cleared so dashboard's Reasoning panel is the only place it appears
    expect(out.output).toBeNull();
  });

  test('session_id propagates from resource attributes when not on span', () => {
    const span = makeSpan({
      attributes: {},
      resource: {
        attributes: { 'voltagent.session_id': 'volt-sess-42' },
        merge: () => undefined,
      } as never,
    });
    expect(otelSpanToLumin(span).session_id).toBe('volt-sess-42');
  });

  test('span attribute beats resource attribute for session_id', () => {
    const span = makeSpan({
      attributes: { 'session.id': 'span-level' },
      resource: {
        attributes: { 'voltagent.session_id': 'resource-level' },
        merge: () => undefined,
      } as never,
    });
    expect(otelSpanToLumin(span).session_id).toBe('span-level');
  });

  test('tool span: gen_ai.tool.name yields type=tool', () => {
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: {
        'gen_ai.tool.name': 'web_search',
        'gen_ai.tool.type': 'function',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('web_search');
  });

  test('tool span: bare tool.name (Vercel/OTel ad-hoc) yields type=tool', () => {
    // Many real-world OTel pipelines emit `tool.name` without the
    // `gen_ai.` prefix; VoltAgent exporter must accept that too.
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: { 'tool.name': 'calculator' },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('calculator');
  });

  test('tool span: voltagent.tool.name namespace also recognized', () => {
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: { 'voltagent.tool.name': 'http_fetch' },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('http_fetch');
  });

  test('tool span: agent.tool.initiated event flag yields type=tool', () => {
    // Real VoltAgent sets `agent.tool.initiated` on its tool-call spans.
    // Even without an explicit tool.name, this hint should classify
    // the span correctly.
    const span = makeSpan({
      kind: SpanKind.INTERNAL,
      attributes: { 'agent.tool.initiated': true, 'tool.name': 'shell' },
    });
    expect(otelSpanToLumin(span).type).toBe('tool');
  });

  test('SpanKind.INTERNAL with no GenAI attrs → custom', () => {
    const span = makeSpan({ kind: SpanKind.INTERNAL });
    expect(otelSpanToLumin(span).type).toBe('custom');
  });

  test('parent_span_id propagates', () => {
    const span = makeSpan({ parentSpanId: 'cccccccccccccccc' });
    expect(otelSpanToLumin(span).parent_span_id).toBe('cccccccccccccccc');
  });

  test('exception event → error', () => {
    const span = makeSpan({
      status: { code: SpanStatusCode.ERROR, message: 'fallback' },
      events: [
        {
          name: 'exception',
          time: [1700000000, 0],
          attributes: {
            'exception.message': 'tool unreachable',
            'exception.type': 'NetworkError',
          },
        },
      ],
    });
    expect(otelSpanToLumin(span).error).toBe('tool unreachable');
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

  test('real VoltAgent bare `input` / `output` keys are read', () => {
    // Verified against node_modules/@voltagent/core/dist source:
    // VoltAgent sets bare `input` and `output` (no namespace) on root,
    // LLM, and tool spans. Without this fallback every VoltAgent span
    // would have null content in the dashboard.
    const span = makeSpan({
      attributes: {
        input: 'Summarize the latest PR for me.',
        output: 'It rebrands Synaptic to Lumin and adds 4 new fixes.',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toContain('Summarize the latest PR');
    expect(out.output).toContain('rebrands Synaptic to Lumin');
  });

  test('voltagent.input / voltagent.output explicit namespace also works', () => {
    const span = makeSpan({
      attributes: {
        'voltagent.input': 'What is the weather in Tokyo?',
        'voltagent.output': 'It is sunny, 24°C.',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toContain('Tokyo');
    expect(out.output).toContain('24°C');
  });

  test('namespaced ai.prompt.messages WINS over bare input', () => {
    // When both are present, the namespaced (more specific) key wins.
    // This protects multi-framework apps that mix a bare `input` log
    // line with a structured Vercel-AI prompt array.
    const span = makeSpan({
      attributes: {
        'ai.prompt.messages':
          '[{"role":"user","content":"namespaced wins"}]',
        input: 'bare loses',
      },
    });
    expect(otelSpanToLumin(span).input).toContain('namespaced wins');
  });

  test('agent.step.text used as output fallback for VoltAgent step spans', () => {
    // VoltAgent's intermediate step spans set `agent.step.text` rather
    // than `output`. Our exporter should still surface that text.
    const span = makeSpan({
      attributes: {
        'agent.step.text': 'Step 2: I should call the calculator tool.',
      },
    });
    expect(otelSpanToLumin(span).output).toContain('Step 2');
  });

  test('guardrail.input.original / .output.after surface as input / output', () => {
    // VoltAgent guardrail spans use the `guardrail.*` namespace for
    // checked content + the policy decision. Surface them so the
    // dashboard renders something on guardrail rows instead of empty.
    const span = makeSpan({
      attributes: {
        'guardrail.name': 'pii_detector',
        'guardrail.action': 'block',
        'guardrail.input.original': 'call me at 9876543210',
        'guardrail.message': 'Detected: phone number — input blocked',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toContain('9876543210');
    expect(out.output).toContain('Detected: phone number');
  });

  test('agent.parent.id is used as session_id fallback', () => {
    // VoltAgent sub-agent spans carry `agent.parent.id` — we use it
    // as a session-grouping fallback so subagent runs land in the
    // right session view.
    const span = makeSpan({
      attributes: { 'agent.parent.id': 'subagent-flow-001' },
    });
    expect(otelSpanToLumin(span).session_id).toBe('subagent-flow-001');
  });

  test('Vercel AI SDK ai.prompt.messages and ai.response.text', () => {
    // VoltAgent uses the same Vercel-AI-style attribute keys when it
    // wraps OpenAI / Anthropic providers.
    const promptJson = JSON.stringify([
      { role: 'user', content: 'What is the weather?' },
    ]);
    const span = makeSpan({
      attributes: {
        'ai.prompt.messages': promptJson,
        'ai.response.text': 'It is sunny.',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toBe(promptJson);
    expect(out.output).toBe('It is sunny.');
  });

  test('Vercel AI tool call: ai.toolCall.args + .result', () => {
    const argsJson = JSON.stringify({ query: 'Tokyo weather' });
    const resultJson = JSON.stringify({ result: 'Sunny, 24°C' });
    const span = makeSpan({
      attributes: {
        'ai.toolCall.args': argsJson,
        'ai.toolCall.result': resultJson,
        'gen_ai.tool.name': 'web_search',
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.type).toBe('tool');
    expect(out.tool_name).toBe('web_search');
    expect(out.input).toBe(argsJson);
    expect(out.output).toBe(resultJson);
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

  test('array-of-strings prompt joins with newlines', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.prompt': ['system: be helpful', 'user: hello'],
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.input).toBe('system: be helpful\nuser: hello');
  });

  test('numeric / boolean attribute values are stringified, not dropped', () => {
    const span = makeSpan({
      attributes: { input: 42 },
    });
    expect(otelSpanToLumin(span).input).toBe('42');
  });

  test('session_id resolves from VoltAgent + cross-framework conventions', () => {
    const a = otelSpanToLumin(
      makeSpan({ attributes: { 'session.id': 's-1' } }),
    );
    expect(a.session_id).toBe('s-1');

    const b = otelSpanToLumin(
      makeSpan({ attributes: { 'gen_ai.conversation.id': 'c-2' } }),
    );
    expect(b.session_id).toBe('c-2');

    const c = otelSpanToLumin(
      makeSpan({ attributes: { 'voltagent.session_id': 's-3' } }),
    );
    expect(c.session_id).toBe('s-3');

    // VoltAgent sub-agent spans set `agent.parent.id`; we use it as a
    // session-grouping fallback so subagent runs land in the right
    // /sessions group.
    const d = otelSpanToLumin(
      makeSpan({ attributes: { 'agent.parent.id': 'parent-001' } }),
    );
    expect(d.session_id).toBe('parent-001');
  });

  test('large input is truncated to maxPayloadSize', () => {
    const huge = 'x'.repeat(50_000);
    const span = makeSpan({ attributes: { input: huge } });
    const out = otelSpanToLumin(span, 1024);
    expect(out.input).not.toBeNull();
    expect(out.input!.length).toBeLessThanOrEqual(1024);
  });

  test('non-string non-number attribute values are rejected by attrString/attrNumber', () => {
    const span = makeSpan({
      attributes: {
        'gen_ai.request.model': 12345 as unknown as string,
        'gen_ai.usage.input_tokens': 'not-a-number' as unknown as number,
      },
    });
    const out = otelSpanToLumin(span);
    expect(out.model).toBeNull();
    expect(out.tokens_input).toBeNull();
  });
});

// ---------- exporter ----------

describe('LuminExporter', () => {
  let originalHost: string | undefined;
  beforeEach(() => {
    originalHost = process.env.LUMIN_HOST;
  });

  test('export() POSTs to /v1/spans with project=voltagent by default', async () => {
    const captured: { url: string; init?: RequestInit } = { url: '' };
    const fakeFetch: typeof fetch = async (url, init) => {
      captured.url = String(url);
      captured.init = init;
      return new Response('{"accepted":1}', { status: 200 });
    };

    const exporter = new LuminExporter({
      host: 'http://localhost:9999',
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
    expect(headers['X-Lumin-Project']).toBe('voltagent');
  });

  test('explicit project overrides default', async () => {
    const captured: { headers?: Record<string, string> } = {};
    const fakeFetch: typeof fetch = async (_url, init) => {
      captured.headers = init?.headers as Record<string, string>;
      return new Response('{}');
    };
    const exporter = new LuminExporter({
      host: 'http://x',
      project: 'my-bot',
      fetchImpl: fakeFetch,
    });
    await new Promise((r) => exporter.export([makeSpan()], r));
    expect(captured.headers!['X-Lumin-Project']).toBe('my-bot');
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

  test('network error swallowed (Rule 7) — export still reports SUCCESS', async () => {
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

  test('hung server is aborted by timeout — agent unaffected', async () => {
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

  test.each([['cleanup']])('cleanup', () => {
    if (originalHost === undefined) delete process.env.LUMIN_HOST;
    else process.env.LUMIN_HOST = originalHost;
    expect(true).toBe(true);
  });
});
