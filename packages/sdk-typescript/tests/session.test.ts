import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { session, Session, withSession } from '../src/session.js';
import { trace } from '../src/trace.js';
import { getCurrentSession } from '../src/context.js';
import { setSDK } from '../src/sdk.js';
import { CapturingExporter, makeTestSDK } from './helpers.js';

// ---------- Session class basics ----------

describe('Session', () => {
  it('uses explicit id when provided', () => {
    const s = new Session({ id: 'user-123-conv-456' });
    expect(s.id).toBe('user-123-conv-456');
  });

  it('builds a slug-prefixed id from a name', () => {
    const s = new Session({ name: 'Booking Flow!' });
    expect(s.id.startsWith('booking-flow-')).toBe(true);
    expect(s.id.length).toBe('booking-flow-'.length + 8);
  });

  it('falls back to a fresh UUID when nothing provided', () => {
    const s = new Session();
    // UUID v4: 36 chars, four hyphens
    expect(s.id).toHaveLength(36);
    expect(s.id.split('-')).toHaveLength(5);
  });

  it('explicit id wins over name', () => {
    const s = new Session({ id: 'explicit', name: 'ignored' });
    expect(s.id).toBe('explicit');
  });
});

// ---------- Propagation through trace() ----------

describe('session propagation', () => {
  let exporter: CapturingExporter;
  let sdk: ReturnType<typeof makeTestSDK>['sdk'];

  beforeEach(() => {
    ({ sdk, exporter } = makeTestSDK());
  });

  afterEach(async () => {
    await sdk.shutdown();
    setSDK(null);
  });

  it('session.run installs session_id on traced spans inside', async () => {
    const myAgent = trace(async (q: unknown) => `answer: ${q}`, { name: 'agent' });
    const s = session({ id: 'user-1-conv-1' });

    await s.run(async () => {
      await myAgent('hello');
      await myAgent('again');
    });
    await sdk.flush();

    expect(exporter.spans).toHaveLength(2);
    for (const span of exporter.spans) {
      expect(span.session_id).toBe('user-1-conv-1');
    }
  });

  it('nested traced functions inherit session id from parent', async () => {
    const child = trace(async () => 'c', { name: 'child' });
    const parent = trace(async () => child(), { name: 'parent' });

    await session({ id: 'nested-test' }).run(async () => {
      await parent();
    });
    await sdk.flush();

    const byName = Object.fromEntries(exporter.spans.map((s) => [s.name, s]));
    expect(byName['parent'].session_id).toBe('nested-test');
    expect(byName['child'].session_id).toBe('nested-test');
  });

  it('outside session.run, session_id is null', async () => {
    const fn = trace(async () => 1, { name: 'standalone' });
    await fn();
    await sdk.flush();
    expect(exporter.spans[0].session_id).toBeNull();
  });

  it('explicit sessionId on trace() overrides session.run context', async () => {
    const fn = trace(async () => 1, {
      name: 'pinned',
      sessionId: 'from-decorator',
    });

    await session({ id: 'from-context' }).run(async () => {
      await fn();
    });
    await sdk.flush();

    expect(exporter.spans[0].session_id).toBe('from-decorator');
  });

  it('explicit sessionId works outside any session.run', async () => {
    const fn = trace(async () => 1, {
      name: 'pinned',
      sessionId: 'explicit-only',
    });
    await fn();
    await sdk.flush();
    expect(exporter.spans[0].session_id).toBe('explicit-only');
  });

  it('getCurrentSession returns the active session inside run()', async () => {
    expect(getCurrentSession()).toBeUndefined();

    const s = session({ id: 'check' });
    let inside: Session | undefined;
    await s.run(async () => {
      inside = getCurrentSession();
    });

    expect(inside).toBe(s);
    expect(getCurrentSession()).toBeUndefined();
  });

  it('withSession is a one-line shortcut', async () => {
    const fn = trace(async () => 1, { name: 'in-with-session' });
    await withSession({ id: 'short' }, async () => {
      await fn();
    });
    await sdk.flush();
    expect(exporter.spans[0].session_id).toBe('short');
  });

  it('nested sessions: inner wins inside, outer restored after', async () => {
    const fn = trace(async () => 1, { name: 'nest' });

    await session({ id: 'outer' }).run(async () => {
      await fn(); // outer
      await session({ id: 'inner' }).run(async () => {
        await fn(); // inner
      });
      await fn(); // outer again
    });
    await sdk.flush();

    const sessions = exporter.spans.map((s) => s.session_id);
    expect(sessions).toEqual(['outer', 'inner', 'outer']);
  });

  it('toJSON includes session_id', async () => {
    const fn = trace(async () => 1, { name: 'shape-check' });
    await withSession({ id: 'in-json' }, async () => {
      await fn();
    });
    await sdk.flush();
    const dict = exporter.spans[0].toJSON();
    expect(dict).toHaveProperty('session_id', 'in-json');
  });

  it('toJSON has session_id null when no session', async () => {
    const fn = trace(async () => 1, { name: 'no-session' });
    await fn();
    await sdk.flush();
    expect(exporter.spans[0].toJSON()).toHaveProperty('session_id', null);
  });

  it('concurrent tasks under one session all share the id', async () => {
    const fn = trace(async (n: unknown) => n, { name: 'task' });
    await withSession({ id: 'concurrent' }, async () => {
      await Promise.all([fn('a'), fn('b'), fn('c')]);
    });
    await sdk.flush();
    expect(exporter.spans).toHaveLength(3);
    for (const s of exporter.spans) {
      expect(s.session_id).toBe('concurrent');
    }
  });
});
