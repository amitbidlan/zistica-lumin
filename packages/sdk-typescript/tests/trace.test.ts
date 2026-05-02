import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { trace, span, withSpan } from '../src/trace.js';
import { setSDK } from '../src/sdk.js';
import { CapturingExporter, makeTestSDK } from './helpers.js';

describe('trace()', () => {
  let exporter: CapturingExporter;
  let sdk: ReturnType<typeof makeTestSDK>['sdk'];

  beforeEach(() => {
    ({ sdk, exporter } = makeTestSDK());
  });

  afterEach(async () => {
    await sdk.shutdown();
    setSDK(null);
  });

  it('records a span around an async function', async () => {
    const greeter = trace(
      async (x: unknown) => `hi ${x}`,
      { name: 'greeter' },
    );
    const result = await greeter('alice');
    await sdk.flush();

    expect(result).toBe('hi alice');
    expect(exporter.spans).toHaveLength(1);
    const s = exporter.spans[0];
    expect(s.name).toBe('greeter');
    expect(s.error).toBeNull();
    expect(s.parent_span_id).toBeNull();
    expect(s.trace_id).toBe(s.id);
    expect(s.ended_at).not.toBeNull();
  });

  it('serializes args as input and return as output', async () => {
    const fn = trace(
      async (a: unknown, b: unknown) => (a as number) + (b as number),
      { name: 'add' },
    );
    await fn(2, 3);
    await sdk.flush();

    const s = exporter.spans[0];
    const inp = JSON.parse(s.input!);
    expect(inp).toEqual({ args: [2, 3] });
    expect(JSON.parse(s.output!)).toBe(5);
  });

  it('records exceptions and re-throws', async () => {
    const boom = trace(async () => {
      throw new Error('bad');
    });
    await expect(boom()).rejects.toThrow('bad');
    await sdk.flush();

    const s = exporter.spans[0];
    expect(s.error).toContain('Error');
    expect(s.error).toContain('bad');
  });

  it('uses fn.name when no explicit name given', async () => {
    async function namedAgent() {
      return 'ok';
    }
    const wrapped = trace(namedAgent);
    await wrapped();
    await sdk.flush();
    expect(exporter.spans[0].name).toBe('namedAgent');
  });
});

describe('manual span()', () => {
  let exporter: CapturingExporter;
  let sdk: ReturnType<typeof makeTestSDK>['sdk'];

  beforeEach(() => {
    ({ sdk, exporter } = makeTestSDK());
  });

  afterEach(async () => {
    await sdk.shutdown();
    setSDK(null);
  });

  it('records a span via try/finally', async () => {
    const s = span('retrieval', { type: 'retrieval' });
    s.setInput({ q: 'hi' });
    s.setOutput({ count: 2 });
    s.end();

    await sdk.flush();
    const captured = exporter.spans[0];
    expect(captured.name).toBe('retrieval');
    expect(captured.type).toBe('retrieval');
    expect(JSON.parse(captured.input!)).toEqual({ q: 'hi' });
    expect(JSON.parse(captured.output!)).toEqual({ count: 2 });
  });

  it('end() is idempotent — submitting twice does not duplicate', async () => {
    const s = span('x');
    s.end();
    s.end();
    await sdk.flush();
    expect(exporter.spans).toHaveLength(1);
  });
});

describe('withSpan()', () => {
  let exporter: CapturingExporter;
  let sdk: ReturnType<typeof makeTestSDK>['sdk'];

  beforeEach(() => {
    ({ sdk, exporter } = makeTestSDK());
  });

  afterEach(async () => {
    await sdk.shutdown();
    setSDK(null);
  });

  it('runs fn inside a span and captures output', async () => {
    const result = await withSpan('block', async () => 42);
    await sdk.flush();
    expect(result).toBe(42);
    expect(exporter.spans[0].name).toBe('block');
  });
});
