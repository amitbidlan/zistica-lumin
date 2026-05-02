import { describe, it, expect } from 'vitest';
import { BoundedQueue } from '../src/queue.js';
import { Span } from '../src/span.js';

describe('BoundedQueue', () => {
  it('drops new items on overflow and counts drops', () => {
    const q = new BoundedQueue(3);
    const spans = Array.from({ length: 5 }, (_, i) => Span.create(`s${i}`));
    const results = spans.map((s) => q.put(s));
    expect(results).toEqual([true, true, true, false, false]);
    expect(q.size).toBe(3);
    expect(q.dropped).toBe(2);
  });

  it('drain returns all items by default and empties the queue', () => {
    const q = new BoundedQueue(10);
    [Span.create('a'), Span.create('b'), Span.create('c')].forEach((s) =>
      q.put(s),
    );
    const got = q.drain();
    expect(got.map((s) => s.name)).toEqual(['a', 'b', 'c']);
    expect(q.size).toBe(0);
  });

  it('drain respects maxItems', () => {
    const q = new BoundedQueue(10);
    [Span.create('a'), Span.create('b'), Span.create('c')].forEach((s) =>
      q.put(s),
    );
    expect(q.drain(2).map((s) => s.name)).toEqual(['a', 'b']);
    expect(q.size).toBe(1);
  });

  it('put never throws', () => {
    const q = new BoundedQueue(1);
    q.put(Span.create('a'));
    expect(() => q.put(Span.create('b'))).not.toThrow();
  });
});
