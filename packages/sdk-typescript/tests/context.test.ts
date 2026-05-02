import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { trace, withSpan } from '../src/trace.js';
import { getCurrentSpan } from '../src/context.js';
import { setSDK } from '../src/sdk.js';
import { CapturingExporter, makeTestSDK } from './helpers.js';

describe('AsyncLocalStorage context propagation', () => {
  let exporter: CapturingExporter;
  let sdk: ReturnType<typeof makeTestSDK>['sdk'];

  beforeEach(() => {
    ({ sdk, exporter } = makeTestSDK());
  });

  afterEach(async () => {
    await sdk.shutdown();
    setSDK(null);
  });

  it('nested traced functions: child has parent_span_id of parent', async () => {
    const child = trace(async () => 'c', { name: 'child' });
    const parent = trace(async () => child(), { name: 'parent' });

    await parent();
    await sdk.flush();

    const byName = Object.fromEntries(exporter.spans.map((s) => [s.name, s]));
    expect(byName['parent'].parent_span_id).toBeNull();
    expect(byName['child'].parent_span_id).toBe(byName['parent'].id);
    expect(byName['child'].trace_id).toBe(byName['parent'].trace_id);
  });

  it('three levels of nesting: ids chain correctly', async () => {
    const l3 = trace(async () => 3, { name: 'l3' });
    const l2 = trace(async () => l3(), { name: 'l2' });
    const l1 = trace(async () => l2(), { name: 'l1' });
    await l1();
    await sdk.flush();

    const m = Object.fromEntries(exporter.spans.map((s) => [s.name, s]));
    expect(m['l1'].parent_span_id).toBeNull();
    expect(m['l2'].parent_span_id).toBe(m['l1'].id);
    expect(m['l3'].parent_span_id).toBe(m['l2'].id);
    expect(m['l1'].trace_id).toBe(m['l2'].trace_id);
    expect(m['l2'].trace_id).toBe(m['l3'].trace_id);
  });

  it('context survives await boundaries', async () => {
    const fn = trace(async () => {
      // Force an await that runs on a microtask
      await new Promise((r) => setTimeout(r, 1));
      const cur = getCurrentSpan();
      return cur?.name;
    }, { name: 'after_await' });

    const result = await fn();
    expect(result).toBe('after_await');
  });

  it('concurrent async tasks have isolated contexts', async () => {
    const inner = trace(async () => getCurrentSpan()?.name, { name: 'inner' });
    const a = trace(async () => {
      await new Promise((r) => setTimeout(r, 5));
      return inner();
    }, { name: 'task_a' });
    const b = trace(async () => {
      await new Promise((r) => setTimeout(r, 1));
      return inner();
    }, { name: 'task_b' });

    await Promise.all([a(), b()]);
    await sdk.flush();

    const inners = exporter.spans.filter((s) => s.name === 'inner');
    const aRoot = exporter.spans.find((s) => s.name === 'task_a')!;
    const bRoot = exporter.spans.find((s) => s.name === 'task_b')!;
    expect(aRoot.trace_id).not.toBe(bRoot.trace_id);

    // Each inner span must be a child of its own task root, not the other
    const traceIds = new Set(inners.map((s) => s.trace_id));
    expect(traceIds.has(aRoot.trace_id)).toBe(true);
    expect(traceIds.has(bRoot.trace_id)).toBe(true);
  });

  it('after function exits, no current span', async () => {
    const fn = trace(async () => 'ok');
    await fn();
    expect(getCurrentSpan()).toBeUndefined();
  });

  it('withSpan establishes context for nested traced functions', async () => {
    const inner = trace(async () => 'i', { name: 'inner' });
    await withSpan('outer', async () => {
      await inner();
    });
    await sdk.flush();

    const byName = Object.fromEntries(exporter.spans.map((s) => [s.name, s]));
    expect(byName['inner'].parent_span_id).toBe(byName['outer'].id);
    expect(byName['inner'].trace_id).toBe(byName['outer'].trace_id);
  });
});
