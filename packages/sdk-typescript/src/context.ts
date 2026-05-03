import { AsyncLocalStorage } from 'node:async_hooks';
import { Span } from './span.js';
import type { Session } from './session.js';

/** Per-async-task current span. Works across await boundaries, Promise
 *  chains, setTimeout, EventEmitter. Does NOT propagate across worker
 *  threads (separate ALS per thread — consistent with Python contextvars). */
export const spanStorage = new AsyncLocalStorage<Span>();

/** Per-async-task current session. Same propagation semantics as the span
 *  storage. Populated by Session.run() / withSession(). */
export const sessionStorage = new AsyncLocalStorage<Session>();

export function getCurrentSpan(): Span | undefined {
  return spanStorage.getStore();
}

export function getCurrentSession(): Session | undefined {
  return sessionStorage.getStore();
}
