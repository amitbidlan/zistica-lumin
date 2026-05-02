import { describe, it, expect, afterEach } from 'vitest';
import { trace } from '../src/trace.js';
import { SynapticSDK, setSDK } from '../src/sdk.js';
import { resolveConfig } from '../src/config.js';
import { HTTPExporter } from '../src/exporter.js';
import { Span } from '../src/span.js';
import { Exporter } from '../src/exporter.js';

let active: SynapticSDK | null = null;

afterEach(async () => {
  if (active) {
    await active.shutdown();
    active = null;
  }
  setSDK(null);
});

describe('resilience — agent must never see Synaptic errors', () => {
  it('failing exporter does not break the agent', async () => {
    class FailingExporter implements Exporter {
      async export(): Promise<void> {
        throw new Error('network down');
      }
      async close(): Promise<void> {}
    }
    active = new SynapticSDK(
      resolveConfig({ host: 'http://test', flushIntervalMs: 3_600_000 }),
      new FailingExporter(),
    );
    setSDK(active);

    const agent = trace(async (x: unknown) => `got ${x}`);
    const result = await agent('hello');
    expect(result).toBe('got hello');

    // Even an explicit flush must swallow the exporter error
    await expect(active.flush()).resolves.toBeUndefined();
  });

  it('unreachable HTTP server does not break the agent', async () => {
    active = new SynapticSDK(
      resolveConfig({
        host: 'http://127.0.0.1:1', // refuses fast
        flushIntervalMs: 3_600_000,
        exportTimeoutMs: 500,
      }),
    );
    setSDK(active);

    const agent = trace(async (x: number) => x * 2);
    const result = await agent(5);
    expect(result).toBe(10);

    await expect(active.flush()).resolves.toBeUndefined();
  });

  it('exception in user code propagates to caller', async () => {
    active = new SynapticSDK(resolveConfig({ host: 'http://test' }));
    setSDK(active);

    const boom = trace(async () => {
      throw new Error('user error');
    });
    await expect(boom()).rejects.toThrow('user error');
  });

  it('HTTPExporter.export does not throw on connection refusal', async () => {
    const ex = new HTTPExporter('http://127.0.0.1:1', null, 500);
    const s = Span.create('test');
    s.end();
    await expect(ex.export([s])).resolves.toBeUndefined();
    await ex.close();
  });

  it('queue overflow is silent (no throw)', async () => {
    active = new SynapticSDK(
      resolveConfig({
        host: 'http://test',
        flushIntervalMs: 3_600_000,
        maxQueueSize: 2,
      }),
    );
    setSDK(active);

    const agent = trace(async () => 'ok');
    // Submit more than the queue capacity — none should throw
    await Promise.all([agent(), agent(), agent(), agent()]);
    expect(active.droppedCount).toBeGreaterThanOrEqual(2);
  });
});
