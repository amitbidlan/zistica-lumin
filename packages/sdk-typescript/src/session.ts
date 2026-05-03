import { randomUUID } from 'node:crypto';
import { sessionStorage } from './context.js';

export interface SessionOptions {
  id?: string;
  name?: string;
}

function slug(name: string): string {
  const s = name.replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase();
  return s || 'session';
}

/** A logical group of related traces — a multi-turn conversation, a
 *  workflow, etc. JS doesn't have Python-style ``with`` blocks; the
 *  typical pattern is ``await session.run(async () => { ... })``.
 *
 *  Inside the callback, every traced function picks up the session id
 *  via AsyncLocalStorage and tags its root span. Server-side this groups
 *  the resulting traces under one session_id.
 */
export class Session {
  readonly id: string;
  readonly name: string | undefined;

  constructor(opts: SessionOptions = {}) {
    if (opts.id) {
      this.id = opts.id;
    } else if (opts.name) {
      // Slug + 8 hex chars from a fresh UUID (matches the Python SDK)
      const short = randomUUID().replace(/-/g, '').slice(0, 8);
      this.id = `${slug(opts.name)}-${short}`;
    } else {
      this.id = randomUUID();
    }
    this.name = opts.name;
  }

  /** Run a callback with this session installed in AsyncLocalStorage.
   *  All traced functions inside (including transitively) inherit
   *  ``session_id``. */
  run<T>(fn: () => Promise<T> | T): Promise<T> {
    return Promise.resolve(sessionStorage.run(this, fn));
  }
}

/** Lowercase factory matching the Python ``synaptic.session(...)`` form. */
export function session(opts: SessionOptions = {}): Session {
  return new Session(opts);
}

/** Convenience: open a session and run a callback in one expression. */
export async function withSession<T>(
  opts: SessionOptions,
  fn: () => Promise<T> | T,
): Promise<T> {
  return new Session(opts).run(fn);
}
