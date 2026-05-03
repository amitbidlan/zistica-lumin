/**
 * Anthropic SDK integration for Synaptic.
 *
 * Wraps the @anthropic-ai/sdk client's `messages.create` so each Claude
 * call produces:
 *   - a parent `claude_call` span (type=llm) with model, tokens, cost
 *   - a `thinking` child span (subtype=thinking) per response
 *   - a `response` child span (subtype=response)
 *
 * Same wire format as the Python SDK's
 * `synaptic.integrations.anthropic` — the dashboard renders both
 * identically.
 *
 * Usage:
 *
 *     import Anthropic from '@anthropic-ai/sdk';
 *     import { instrumentAnthropic } from '@synaptic/sdk/integrations/anthropic';
 *
 *     const client = new Anthropic();
 *     instrumentAnthropic(client);
 *
 *     await client.messages.create({
 *       model: 'claude-opus-4-20250514',
 *       max_tokens: 16000,
 *       thinking: { type: 'enabled', budget_tokens: 10000 },
 *       messages: [{ role: 'user', content: 'What is 2+2?' }],
 *     });
 */

import { getCurrentSpan } from '../context.js';
import { getSDK } from '../sdk.js';
import { Span } from '../span.js';

/** Pricing in USD per 1k tokens (May 2026, mirrors the Python integration). */
const PRICES_PER_1K: Record<string, [number, number]> = {
  'claude-opus-4': [0.015, 0.075],
  'claude-sonnet-4': [0.003, 0.015],
  'claude-haiku-4': [0.001, 0.005],
};

function computeCost(
  model: string | null | undefined,
  tin: number | null | undefined,
  tout: number | null | undefined,
): number | null {
  if (!model || tin == null || tout == null) return null;
  const m = model.toLowerCase();
  let bestKey = '';
  let best: [number, number] | null = null;
  for (const [key, prices] of Object.entries(PRICES_PER_1K)) {
    if (m.startsWith(key) && key.length > bestKey.length) {
      bestKey = key;
      best = prices;
    }
  }
  if (!best) return null;
  const [inp, outp] = best;
  return Math.round(((tin * inp) / 1000 + (tout * outp) / 1000) * 1e8) / 1e8;
}

/** ~4 chars per English token — same heuristic as the Python SDK. */
function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.max(1, Math.floor(text.length / 4));
}

interface ContentBlock {
  type?: string;
  thinking?: string;
  text?: string;
}

interface AnthropicResponse {
  content?: ContentBlock[];
  usage?: { input_tokens?: number; output_tokens?: number };
}

interface AnthropicMessages {
  create: (...args: unknown[]) => Promise<AnthropicResponse> | AnthropicResponse;
}

interface AnthropicLikeClient {
  messages: AnthropicMessages;
}

const PATCH_MARKER = '__synapticAnthropicWrapped';

function makeChild(parent: Span, name: string, subtype?: string): Span {
  const child = Span.create(name, 'llm', parent);
  child.span_subtype = subtype ?? null;
  child.session_id = parent.session_id;
  return child;
}

function recordCall(
  model: string | null,
  requestMessages: unknown,
  response: AnthropicResponse,
  outerSpan: Span | undefined,
): void {
  try {
    // Parent span — anchored under the surrounding @synaptic.trace if any
    const parent = outerSpan
      ? Span.create('claude_call', 'llm', outerSpan)
      : Span.create('claude_call', 'llm');
    parent.model = model;
    parent.provider = 'anthropic';
    parent.session_id = outerSpan?.session_id ?? null;
    parent.setInput({ messages: requestMessages });

    const content = response.content ?? [];
    const thinkingBlocks = content.filter((b) => b?.type === 'thinking');
    const textBlocks = content.filter((b) => b?.type === 'text');

    const inputTokens = response.usage?.input_tokens ?? null;
    const outputTokens = response.usage?.output_tokens ?? null;
    parent.tokens_input = inputTokens;
    parent.tokens_output = outputTokens;
    parent.cost_usd = computeCost(model, inputTokens, outputTokens);

    const thinkingText = thinkingBlocks
      .map((b) => b.thinking ?? '')
      .join('');
    const thinkingTokensEst = estimateTokens(thinkingText);
    if (thinkingBlocks.length > 0) {
      parent.thinking_tokens = thinkingTokensEst;
    }

    const responseText = textBlocks.map((b) => b.text ?? '').join('');
    parent.setOutput({ text: responseText });
    parent.end();

    const sdk = getSDK();
    sdk.submit(parent);

    if (thinkingBlocks.length > 0) {
      const thinkingSpan = makeChild(parent, 'thinking', 'thinking');
      thinkingSpan.model = model;
      thinkingSpan.provider = 'anthropic';
      thinkingSpan.thinking_tokens = thinkingTokensEst;
      thinkingSpan.setInput({ thinking: thinkingText });
      thinkingSpan.cost_usd = computeCost(model, 0, thinkingTokensEst);
      thinkingSpan.end();
      sdk.submit(thinkingSpan);
    }

    if (textBlocks.length > 0) {
      const responseSpan = makeChild(parent, 'response', 'response');
      responseSpan.model = model;
      responseSpan.provider = 'anthropic';
      // Subtract estimated thinking from output for response-only count
      if (outputTokens != null && thinkingTokensEst) {
        responseSpan.tokens_output = Math.max(
          0,
          outputTokens - thinkingTokensEst,
        );
      } else {
        responseSpan.tokens_output = outputTokens;
      }
      responseSpan.cost_usd = computeCost(model, 0, responseSpan.tokens_output);
      responseSpan.setOutput({ text: responseText });
      responseSpan.end();
      sdk.submit(responseSpan);
    }
  } catch {
    // Best-effort — Synaptic must never break the agent (Rule 7)
  }
}

/**
 * Wrap an Anthropic client's `messages.create` so every Claude call
 * is recorded as a parent + thinking + response trio. Idempotent.
 */
export function instrumentAnthropic(client: AnthropicLikeClient): void {
  const messages = client.messages;
  if (!messages) return;
  const original = messages.create;
  if (typeof original !== 'function') return;
  if ((original as { [PATCH_MARKER]?: boolean })[PATCH_MARKER]) return;

  const wrapped = async function wrapped(
    this: unknown,
    ...args: unknown[]
  ): Promise<AnthropicResponse> {
    const opts = (args[0] ?? {}) as { model?: string; messages?: unknown };
    const outer = getCurrentSpan();
    const result = await Promise.resolve(original.apply(this, args));
    recordCall(opts.model ?? null, opts.messages, result, outer);
    return result;
  };
  Object.defineProperty(wrapped, PATCH_MARKER, { value: true });
  messages.create = wrapped as typeof messages.create;
}

// Exported for unit tests
export const __test__ = { computeCost, estimateTokens, recordCall };
