import { describe, it, expect, afterEach } from 'vitest';
import { createServer, IncomingMessage, ServerResponse, Server } from 'node:http';
import { trace } from '../src/trace.js';
import { LuminSDK, setSDK } from '../src/sdk.js';
import { resolveConfig } from '../src/config.js';

interface RecordedRequest {
  path: string;
  body: unknown;
  headers: Record<string, string | string[] | undefined>;
}

function startStubServer(): Promise<{
  port: number;
  received: RecordedRequest[];
  close(): Promise<void>;
}> {
  return new Promise((resolve) => {
    const received: RecordedRequest[] = [];
    const server: Server = createServer((req: IncomingMessage, res: ServerResponse) => {
      const chunks: Buffer[] = [];
      req.on('data', (c: Buffer) => chunks.push(c));
      req.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf-8');
        let body: unknown = null;
        try {
          body = raw ? JSON.parse(raw) : null;
        } catch {
          body = raw;
        }
        received.push({
          path: req.url ?? '',
          body,
          headers: req.headers as Record<string, string | string[] | undefined>,
        });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{"accepted":1}');
      });
    });
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      resolve({
        port,
        received,
        close: () =>
          new Promise<void>((res) => {
            server.close(() => res());
          }),
      });
    });
  });
}

describe('end-to-end: SDK posts to a real HTTP server', () => {
  let close: (() => Promise<void>) | null = null;
  let sdk: LuminSDK | null = null;

  afterEach(async () => {
    if (sdk) {
      await sdk.shutdown();
      sdk = null;
    }
    setSDK(null);
    if (close) {
      await close();
      close = null;
    }
  });

  it('POST /v1/spans with the right shape', async () => {
    const server = await startStubServer();
    close = server.close;

    sdk = new LuminSDK(
      resolveConfig({
        host: `http://127.0.0.1:${server.port}`,
        flushIntervalMs: 3_600_000,
        exportTimeoutMs: 2_000,
      }),
    );
    setSDK(sdk);

    const agent = trace(
      async (x: unknown) => `hello ${x}`,
      { name: 'my_agent' },
    );
    const result = await agent('world');
    expect(result).toBe('hello world');

    await sdk.flush();

    expect(server.received).toHaveLength(1);
    const req = server.received[0];
    expect(req.path).toBe('/v1/spans');
    expect(req.headers['content-type']).toBe('application/json');

    const body = req.body as { spans: Record<string, unknown>[] };
    expect(body.spans).toHaveLength(1);
    const span = body.spans[0];
    for (const k of [
      'id',
      'trace_id',
      'parent_span_id',
      'name',
      'type',
      'input',
      'output',
      'started_at',
      'ended_at',
      'error',
    ]) {
      expect(span).toHaveProperty(k);
    }
    expect(span.name).toBe('my_agent');
    expect(span.parent_span_id).toBeNull();
    expect(span.trace_id).toBe(span.id);
    expect(JSON.parse(span.input as string)).toEqual({ args: ['world'] });
    expect(JSON.parse(span.output as string)).toBe('hello world');
  });

  it('shutdown drains pending spans even without explicit flush', async () => {
    const server = await startStubServer();
    close = server.close;

    sdk = new LuminSDK(
      resolveConfig({
        host: `http://127.0.0.1:${server.port}`,
        flushIntervalMs: 3_600_000, // background flusher would never fire
        exportTimeoutMs: 2_000,
      }),
    );
    setSDK(sdk);

    const fn = trace(async () => 'ok', { name: 'quick' });
    await fn();
    // No explicit flush — rely on shutdown to drain.
    await sdk.shutdown();

    expect(server.received).toHaveLength(1);
    expect(server.received[0].path).toBe('/v1/spans');
  });
});
