import { Span } from './span.js';

export interface Exporter {
  export(spans: Span[]): Promise<void>;
  close(): Promise<void>;
}

/** Posts spans to {host}/v1/spans. Swallows every error — the agent
 *  must never see a failure caused by Synaptic. */
export class HTTPExporter implements Exporter {
  private readonly url: string;
  private readonly headers: Record<string, string>;
  private readonly timeoutMs: number;

  constructor(host: string, apiKey: string | null, timeoutMs: number) {
    this.url = host.replace(/\/$/, '') + '/v1/spans';
    this.headers = { 'Content-Type': 'application/json' };
    if (apiKey) this.headers['X-API-Key'] = apiKey;
    this.timeoutMs = timeoutMs;
  }

  async export(spans: Span[]): Promise<void> {
    if (spans.length === 0) return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const body = JSON.stringify({ spans: spans.map((s) => s.toJSON()) });
      await fetch(this.url, {
        method: 'POST',
        headers: this.headers,
        body,
        signal: ctrl.signal,
      });
    } catch {
      // Drop silently. Connection refused, timeout, DNS — none of it should
      // ever propagate into the agent code.
    } finally {
      clearTimeout(timer);
    }
  }

  async close(): Promise<void> {
    /* fetch has no resources to release */
  }
}
