import { describe, expect, test } from 'vitest';
import {
  __test__,
  instrumentAnthropic,
} from '../src/integrations/anthropic.js';
import { trace } from '../src/trace.js';
import { makeTestSDK } from './helpers.js';

const { computeCost, estimateTokens, recordCall } = __test__;

function fakeResponse(opts: {
  thinking?: string;
  text?: string;
  inputTokens?: number;
  outputTokens?: number;
}) {
  const content: Array<{ type: string; thinking?: string; text?: string }> = [];
  if (opts.thinking !== undefined)
    content.push({ type: 'thinking', thinking: opts.thinking });
  if (opts.text !== undefined) content.push({ type: 'text', text: opts.text });
  return {
    content,
    usage: {
      input_tokens: opts.inputTokens ?? 10,
      output_tokens: opts.outputTokens ?? 20,
    },
  };
}

async function drain(sdk: ReturnType<typeof makeTestSDK>['sdk']) {
  await sdk.flush();
}

// ---------- pricing math ----------

describe('computeCost', () => {
  test('opus 1k/1k matches Python implementation', () => {
    expect(computeCost('claude-opus-4-20250514', 1000, 1000)).toBeCloseTo(
      0.09,
      6,
    );
  });
  test('unknown model returns null', () => {
    expect(computeCost('not-a-claude', 100, 100)).toBeNull();
  });
  test('zero input still computes for thinking spans (output rate)', () => {
    expect(computeCost('claude-opus-4', 0, 1000)).toBeCloseTo(0.075, 6);
  });
  test('null tokens return null', () => {
    expect(computeCost('claude-opus-4', null, 100)).toBeNull();
    expect(computeCost('claude-opus-4', 100, null)).toBeNull();
  });
});

describe('estimateTokens', () => {
  test('empty returns 0', () => {
    expect(estimateTokens('')).toBe(0);
  });
  test('uses ~4 chars per token', () => {
    expect(estimateTokens('a'.repeat(40))).toBe(10);
  });
});

// ---------- core integration ----------

describe('recordCall', () => {
  test('emits parent + thinking + response with correct relationships', async () => {
    const { sdk, exporter } = makeTestSDK();
    const response = fakeResponse({
      thinking: 'Let me reason: 2+2 means combining two and two.',
      text: '2+2 equals 4.',
      inputTokens: 12,
      outputTokens: 400,
    });

    recordCall('claude-opus-4-20250514', [], response, undefined);
    await drain(sdk);

    const byName = Object.fromEntries(
      exporter.spans.map((s) => [s.name, s]),
    );
    expect(byName.claude_call).toBeDefined();
    expect(byName.thinking).toBeDefined();
    expect(byName.response).toBeDefined();

    const parent = byName.claude_call;
    expect(byName.thinking.parent_span_id).toBe(parent.id);
    expect(byName.response.parent_span_id).toBe(parent.id);
    expect(byName.thinking.trace_id).toBe(parent.trace_id);

    expect(byName.thinking.span_subtype).toBe('thinking');
    expect(byName.response.span_subtype).toBe('response');
    expect(byName.thinking.thinking_tokens).toBeGreaterThan(0);
    expect(byName.thinking.cost_usd).toBeGreaterThan(0);
    expect(byName.thinking.input).toContain('combining');
    expect(byName.response.output).toContain('4');
  });

  test('no thinking blocks: only parent + response emitted', async () => {
    const { sdk, exporter } = makeTestSDK();
    recordCall(
      'claude-haiku-4',
      [],
      fakeResponse({ text: 'hello' }),
      undefined,
    );
    await drain(sdk);
    const names = exporter.spans.map((s) => s.name);
    expect(names).toContain('claude_call');
    expect(names).toContain('response');
    expect(names).not.toContain('thinking');
  });

  test('unknown model: cost is null but call still recorded', async () => {
    const { sdk, exporter } = makeTestSDK();
    recordCall(
      'some-future-model',
      [],
      fakeResponse({ thinking: 'r', text: 'a' }),
      undefined,
    );
    await drain(sdk);
    const parent = exporter.spans.find((s) => s.name === 'claude_call')!;
    expect(parent.cost_usd).toBeNull();
    expect(parent.model).toBe('some-future-model');
  });

  test('toJSON serializes the new fields on every span', async () => {
    const { sdk, exporter } = makeTestSDK();
    recordCall(
      'claude-opus-4',
      [],
      fakeResponse({ thinking: 'r', text: 'a' }),
      undefined,
    );
    await drain(sdk);
    for (const s of exporter.spans) {
      const j = s.toJSON();
      expect(j).toHaveProperty('span_subtype');
      expect(j).toHaveProperty('thinking_tokens');
      expect(j).toHaveProperty('model');
      expect(j).toHaveProperty('provider');
      expect(j).toHaveProperty('tokens_input');
      expect(j).toHaveProperty('tokens_output');
      expect(j).toHaveProperty('cost_usd');
    }
  });
});

// ---------- instrument & call ----------

describe('instrumentAnthropic', () => {
  test('wraps client.messages.create idempotently', async () => {
    const { sdk, exporter } = makeTestSDK();
    let invocations = 0;
    const fakeClient = {
      messages: {
        create: async () => {
          invocations += 1;
          return fakeResponse({ thinking: 'r', text: 'a' });
        },
      },
    };

    instrumentAnthropic(fakeClient);
    instrumentAnthropic(fakeClient);
    instrumentAnthropic(fakeClient);

    await fakeClient.messages.create({ model: 'claude-opus-4', messages: [] });
    await sdk.flush();

    expect(invocations).toBe(1);
    const names = exporter.spans.map((s) => s.name).sort();
    expect(names).toEqual(['claude_call', 'response', 'thinking']);
  });

  test('inside trace(), claude_call attaches to outer span', async () => {
    const { sdk, exporter } = makeTestSDK();
    const fakeClient = {
      messages: {
        create: async () =>
          fakeResponse({ thinking: 'reasoning', text: 'answer' }),
      },
    };
    instrumentAnthropic(fakeClient);

    const myAgent = trace(
      async () => {
        await fakeClient.messages.create({
          model: 'claude-opus-4',
          messages: [],
        });
      },
      { name: 'my_agent' },
    );
    await myAgent();
    await sdk.flush();

    const spans = exporter.spans;
    const outer = spans.find((s) => s.name === 'my_agent');
    const claudeCall = spans.find((s) => s.name === 'claude_call');
    const thinking = spans.find((s) => s.name === 'thinking');

    expect(outer).toBeDefined();
    expect(claudeCall).toBeDefined();
    expect(claudeCall!.trace_id).toBe(outer!.trace_id);
    expect(claudeCall!.parent_span_id).toBe(outer!.id);
    expect(thinking!.parent_span_id).toBe(claudeCall!.id);
  });

  test('original create exception propagates and no spans land', async () => {
    const { sdk, exporter } = makeTestSDK();
    const fakeClient = {
      messages: {
        create: async () => {
          throw new Error('rate limited');
        },
      },
    };
    instrumentAnthropic(fakeClient);

    await expect(
      fakeClient.messages.create({ model: 'claude-opus-4', messages: [] }),
    ).rejects.toThrow('rate limited');
    await sdk.flush();
    expect(exporter.spans).toHaveLength(0);
  });
});
