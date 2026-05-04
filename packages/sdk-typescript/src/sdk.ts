import { ResolvedConfig, LuminConfig, defaultConfig, resolveConfig } from './config.js';
import { Exporter, HTTPExporter } from './exporter.js';
import { BoundedQueue } from './queue.js';
import { Span } from './span.js';

export class LuminSDK {
  readonly config: ResolvedConfig;
  private readonly queue: BoundedQueue;
  private exporter: Exporter;
  private flushTimer: NodeJS.Timeout | null = null;
  private shuttingDown = false;

  constructor(config: ResolvedConfig, exporter?: Exporter) {
    this.config = config;
    this.queue = new BoundedQueue(config.maxQueueSize);
    this.exporter =
      exporter ??
      new HTTPExporter(config.host, config.apiKey, config.exportTimeoutMs);
    this.startFlushLoop();
  }

  /** Non-blocking. Returns false if the queue is at capacity. */
  submit(span: Span): boolean {
    if (this.shuttingDown) return false;
    return this.queue.put(span);
  }

  /** Drain the queue and export synchronously (await-able). Used by tests
   *  and by the beforeExit / shutdown paths. */
  async flush(): Promise<void> {
    const batch = this.queue.drain(this.config.batchSize);
    if (batch.length === 0) return;
    try {
      await this.exporter.export(batch);
    } catch {
      /* swallow */
    }
  }

  private startFlushLoop(): void {
    this.flushTimer = setInterval(() => {
      void this.flush();
    }, this.config.flushIntervalMs);
    // Don't keep the Node event loop alive just for the flush timer
    this.flushTimer.unref?.();
  }

  async shutdown(): Promise<void> {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    if (this.flushTimer) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }
    // Drain everything
    let remaining = this.queue.drain();
    while (remaining.length > 0) {
      try {
        await this.exporter.export(remaining);
      } catch {
        /* swallow */
      }
      remaining = this.queue.drain();
    }
    try {
      await this.exporter.close();
    } catch {
      /* swallow */
    }
  }

  /** Test hook — replace the exporter without restarting the SDK. */
  setExporter(exporter: Exporter): void {
    this.exporter = exporter;
  }

  get droppedCount(): number {
    return this.queue.dropped;
  }
}

// --- global singleton ---

let _global: LuminSDK | null = null;

export function getSDK(): LuminSDK {
  if (_global === null) {
    _global = new LuminSDK(defaultConfig());
  }
  return _global;
}

export function setSDK(sdk: LuminSDK | null): LuminSDK | null {
  const old = _global;
  _global = sdk;
  if (old !== null && old !== sdk) {
    void old.shutdown();
  }
  return _global;
}

export function configure(opts: LuminConfig = {}): void {
  setSDK(new LuminSDK(resolveConfig(opts)));
}

// Best-effort flush on process exit. beforeExit fires when the event loop
// drains naturally; the Promise we kick off keeps the loop alive long
// enough to send pending spans.
let exitHookRegistered = false;
function registerExitHook() {
  if (exitHookRegistered) return;
  exitHookRegistered = true;
  process.once('beforeExit', () => {
    if (_global !== null) void _global.shutdown();
  });
}
registerExitHook();
