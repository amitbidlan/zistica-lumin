import { describe, it, expect } from 'vitest';
import { Span } from '../src/span.js';

describe('Span', () => {
  it('root span: trace_id equals id, parent is null', () => {
    const s = Span.create('root');
    expect(s.trace_id).toBe(s.id);
    expect(s.parent_span_id).toBeNull();
  });

  it('child span: inherits parent trace_id, references parent.id', () => {
    const root = Span.create('root');
    const child = Span.create('child', 'custom', root);
    expect(child.trace_id).toBe(root.trace_id);
    expect(child.parent_span_id).toBe(root.id);
    expect(child.id).not.toBe(root.id);
  });

  it('serializes input/output as JSON strings', () => {
    const s = Span.create('x');
    s.setInput({ a: 1, b: 'two' });
    s.setOutput([1, 2, 3]);
    expect(s.input).toBe('{"a":1,"b":"two"}');
    expect(s.output).toBe('[1,2,3]');
  });

  it('truncates oversized payloads to maxSize', () => {
    const s = Span.create('x');
    s.setInput('a'.repeat(20_000), 100);
    expect(s.input!.length).toBe(100);
  });

  it('records error class and message', () => {
    const s = Span.create('x');
    s.setError(new TypeError('bad input'));
    expect(s.error).toContain('TypeError');
    expect(s.error).toContain('bad input');
  });

  it('end() is idempotent', () => {
    const s = Span.create('x');
    s.end();
    const first = s.ended_at;
    s.end();
    expect(s.ended_at).toBe(first);
  });

  it('toJSON has all 10 required fields', () => {
    const s = Span.create('x');
    s.end();
    const d = s.toJSON();
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
      expect(d).toHaveProperty(k);
    }
  });
});
