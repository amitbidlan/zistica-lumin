import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import {
  luminProcessor,
  luminExporter,
  resolveServiceName,
} from '../src/config.js';
import { LuminExporter } from '../src/exporter.js';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';

describe('luminProcessor() / luminExporter() / resolveServiceName()', () => {
  let originalEnv: Record<string, string | undefined>;
  beforeEach(() => {
    originalEnv = {
      LUMIN_HOST: process.env.LUMIN_HOST,
      LUMIN_API_KEY: process.env.LUMIN_API_KEY,
      LUMIN_SERVICE_NAME: process.env.LUMIN_SERVICE_NAME,
    };
  });
  afterEach(() => {
    for (const [k, v] of Object.entries(originalEnv)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  test('luminProcessor returns a BatchSpanProcessor', () => {
    const p = luminProcessor();
    expect(p).toBeInstanceOf(BatchSpanProcessor);
  });

  test('luminExporter returns a LuminExporter', () => {
    expect(luminExporter()).toBeInstanceOf(LuminExporter);
  });

  test('resolveServiceName defaults to voltagent-app', () => {
    delete process.env.LUMIN_SERVICE_NAME;
    expect(resolveServiceName()).toBe('voltagent-app');
  });

  test('resolveServiceName uses LUMIN_SERVICE_NAME env var', () => {
    process.env.LUMIN_SERVICE_NAME = 'my-bot';
    expect(resolveServiceName()).toBe('my-bot');
  });

  test('resolveServiceName explicit option overrides env', () => {
    process.env.LUMIN_SERVICE_NAME = 'env-name';
    expect(resolveServiceName({ serviceName: 'explicit' })).toBe('explicit');
  });

  test('luminExporter wired with the supplied options', async () => {
    const captured: { url?: string; headers?: Record<string, string> } = {};
    const fakeFetch: typeof fetch = async (url, init) => {
      captured.url = String(url);
      captured.headers = init?.headers as Record<string, string>;
      return new Response('{}');
    };
    const exporter = luminExporter({
      host: 'http://my-lumin:8000',
      apiKey: 'k',
      project: 'demo',
      fetchImpl: fakeFetch,
    });
    await new Promise((r) =>
      exporter.export(
        [
          {
            name: 's',
            kind: 1,
            spanContext: () => ({
              traceId: 't',
              spanId: 'a',
              traceFlags: 0,
              isRemote: false,
            }),
            parentSpanId: undefined,
            startTime: [0, 0],
            endTime: [0, 1_000_000],
            status: { code: 0 },
            attributes: {},
            links: [],
            events: [],
            duration: [0, 1_000_000],
            ended: true,
            resource: { attributes: {}, merge: () => undefined } as never,
            instrumentationLibrary: { name: 't', version: '0' },
            droppedAttributesCount: 0,
            droppedEventsCount: 0,
            droppedLinksCount: 0,
          },
        ],
        r as (v: unknown) => void,
      ),
    );
    expect(captured.url).toBe('http://my-lumin:8000/v1/spans');
    expect(captured.headers!['Authorization']).toBe('Bearer k');
    expect(captured.headers!['X-Lumin-Project']).toBe('demo');
  });
});

describe('tryAttach() — pure attachment logic', () => {
  test('attaches when provider exposes addSpanProcessor directly', async () => {
    const { tryAttach } = await import('../src/auto.js');
    let added: unknown = null;
    const provider = {
      getTracer: () => ({}) as never,
      addSpanProcessor: (p: unknown) => {
        added = p;
      },
    };
    expect(tryAttach(provider as never)).toBe(true);
    expect(added).not.toBeNull();
  });

  test('walks getDelegate() chain to find a compatible provider', async () => {
    const { tryAttach } = await import('../src/auto.js');
    let added: unknown = null;
    const delegate = {
      getTracer: () => ({}) as never,
      addSpanProcessor: (p: unknown) => {
        added = p;
      },
    };
    const proxy = {
      getTracer: () => ({}) as never,
      getDelegate: () => delegate,
    };
    expect(tryAttach(proxy as never)).toBe(true);
    expect(added).not.toBeNull();
  });

  test('returns false when no addSpanProcessor reachable (modern OTel)', async () => {
    const { tryAttach } = await import('../src/auto.js');
    const modernProxy = { getTracer: () => ({}) as never };
    expect(tryAttach(modernProxy as never)).toBe(false);
  });

  test('returns false when proxy chain is too deep without a target', async () => {
    const { tryAttach } = await import('../src/auto.js');
    let p: unknown = { getTracer: () => ({}) };
    for (let i = 0; i < 10; i++) {
      const inner = p;
      p = {
        getTracer: () => ({}) as never,
        getDelegate: () => inner,
      };
    }
    expect(tryAttach(p as never)).toBe(false);
  });
});

describe('installLuminTracing() — env gate', () => {
  let originalEnv: string | undefined;
  beforeEach(() => {
    originalEnv = process.env.LUMIN_TRACING;
  });
  afterEach(() => {
    if (originalEnv === undefined) delete process.env.LUMIN_TRACING;
    else process.env.LUMIN_TRACING = originalEnv;
  });

  test('returns false when LUMIN_TRACING is unset', async () => {
    delete process.env.LUMIN_TRACING;
    const { installLuminTracing } = await import('../src/auto.js');
    expect(installLuminTracing()).toBe(false);
  });

  test('returns false when the global provider is the default no-op proxy', async () => {
    process.env.LUMIN_TRACING = 'true';
    const { installLuminTracing } = await import('../src/auto.js');
    expect(installLuminTracing()).toBe(false);
  });
});
