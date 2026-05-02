import { AsyncLocalStorage } from 'node:async_hooks';
import { Span } from './span.js';

/** Per-async-task current span. Works across await boundaries, Promise
 *  chains, setTimeout, EventEmitter. Does NOT propagate across worker
 *  threads (separate ALS per thread — consistent with Python contextvars). */
export const spanStorage = new AsyncLocalStorage<Span>();

export function getCurrentSpan(): Span | undefined {
  return spanStorage.getStore();
}
