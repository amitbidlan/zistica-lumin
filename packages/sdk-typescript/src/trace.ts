import { spanStorage, getCurrentSpan, getCurrentSession } from './context.js';
import { Span } from './span.js';
import { getSDK } from './sdk.js';

export interface TraceOptions {
  name?: string;
  type?: string;
  /** Pin every invocation to a specific session id. Overrides any active
   *  session() context. */
  sessionId?: string;
}

function resolveSessionId(
  explicit: string | undefined,
  parent: Span | null,
): string | null {
  if (explicit) return explicit;
  const current = getCurrentSession();
  if (current) return current.id;
  if (parent && parent.session_id) return parent.session_id;
  return null;
}

/** Wrap an async function so each invocation records a Synaptic span.
 *  The wrapped function preserves the original signature.
 *  Errors propagate to the caller; the span captures them via setError().
 */
export function trace<T extends (...args: unknown[]) => Promise<unknown>>(
  fn: T,
  options: TraceOptions = {},
): T {
  const spanName = options.name ?? fn.name ?? 'anonymous';
  const spanType = options.type ?? 'custom';

  const wrapped = async function (...args: unknown[]): Promise<unknown> {
    const sdk = getSDK();
    const cfg = sdk.config;
    const parent = getCurrentSpan() ?? null;
    const s = Span.create(spanName, spanType, parent);
    s.session_id = resolveSessionId(options.sessionId, parent);

    if (cfg.captureInputs) {
      // Match Python SDK: capture as { args: [...] }
      s.setInput({ args }, cfg.maxPayloadSize);
    }

    return spanStorage.run(s, async () => {
      try {
        const result = await fn(...args);
        if (cfg.captureOutputs) {
          s.setOutput(result, cfg.maxPayloadSize);
        }
        return result;
      } catch (e) {
        s.setError(e);
        throw e;
      } finally {
        s.end();
        sdk.submit(s);
      }
    });
  };

  return wrapped as unknown as T;
}

export interface SpanHandle {
  readonly id: string;
  readonly trace_id: string;
  setInput(value: unknown): void;
  setOutput(value: unknown): void;
  setError(err: unknown): void;
  end(): void;
}

/** Manual span — caller controls lifecycle via .end(). Does NOT install
 *  itself in the AsyncLocalStorage automatically; use `withSpan` if you
 *  want nested calls to see this as their parent. */
export function span(name: string, options: { type?: string } = {}): SpanHandle {
  const sdk = getSDK();
  const parent = getCurrentSpan() ?? null;
  const s = Span.create(name, options.type ?? 'custom', parent);
  s.session_id = resolveSessionId(undefined, parent);
  let ended = false;

  return {
    get id() {
      return s.id;
    },
    get trace_id() {
      return s.trace_id;
    },
    setInput(v: unknown) {
      s.setInput(v, sdk.config.maxPayloadSize);
    },
    setOutput(v: unknown) {
      s.setOutput(v, sdk.config.maxPayloadSize);
    },
    setError(err: unknown) {
      s.setError(err);
    },
    end() {
      if (ended) return;
      ended = true;
      s.end();
      sdk.submit(s);
    },
  };
}

/** Run `fn` inside a span — propagates context to nested calls. */
export async function withSpan<T>(
  name: string,
  fn: () => Promise<T>,
  options: { type?: string } = {},
): Promise<T> {
  const sdk = getSDK();
  const parent = getCurrentSpan() ?? null;
  const s = Span.create(name, options.type ?? 'custom', parent);
  s.session_id = resolveSessionId(undefined, parent);

  return spanStorage.run(s, async () => {
    try {
      const result = await fn();
      if (sdk.config.captureOutputs) {
        s.setOutput(result, sdk.config.maxPayloadSize);
      }
      return result;
    } catch (e) {
      s.setError(e);
      throw e;
    } finally {
      s.end();
      sdk.submit(s);
    }
  });
}
