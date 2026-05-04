import { Exporter } from '../src/exporter.js';
import { Span } from '../src/span.js';
import { LuminSDK, setSDK } from '../src/sdk.js';
import { resolveConfig } from '../src/config.js';

export class CapturingExporter implements Exporter {
  spans: Span[] = [];
  async export(spans: Span[]): Promise<void> {
    this.spans.push(...spans);
  }
  async close(): Promise<void> {
    /* no-op */
  }
}

export function makeTestSDK(): { sdk: LuminSDK; exporter: CapturingExporter } {
  const exporter = new CapturingExporter();
  // Very high flush interval so the background timer never runs during tests
  // — tests call sdk.flush() explicitly to drain.
  const cfg = resolveConfig({
    host: 'http://test',
    flushIntervalMs: 3_600_000,
    exportTimeoutMs: 500,
  });
  const sdk = new LuminSDK(cfg, exporter);
  setSDK(sdk);
  return { sdk, exporter };
}
