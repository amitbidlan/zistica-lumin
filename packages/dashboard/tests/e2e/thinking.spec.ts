/**
 * Browser-level E2E tests for Claude extended-thinking visualization.
 *
 * Posts a trace shaped like a Claude call with a thinking block + a
 * response block, navigates to the trace detail page, and asserts the
 * dashboard renders the thinking row with brain emoji, badge, token
 * estimate, and an expandable reasoning panel — plus the trace-level
 * thinking-vs-response cost breakdown.
 */
import { test, expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const API_BASE = process.env.E2E_API_URL ?? 'http://localhost:8000';

async function postSpans(spans: Array<Record<string, unknown>>): Promise<void> {
  const res = await fetch(`${API_BASE}/v1/spans`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spans }),
  });
  if (!res.ok) {
    throw new Error(`POST /v1/spans failed: ${res.status} ${await res.text()}`);
  }
}

test('thinking span renders with brain emoji, badge, and expandable reasoning', async ({ page }) => {
  const traceId = `e2e-thinking-${randomUUID()}`;
  const parentId = `parent-${randomUUID()}`;
  const thinkingId = `thinking-${randomUUID()}`;
  const responseId = `response-${randomUUID()}`;
  const reasoningText = 'Let me carefully reason through 2+2: combining two units...';

  await postSpans([
    {
      id: parentId,
      trace_id: traceId,
      name: 'claude_call',
      type: 'llm',
      started_at: '2026-05-03T10:00:00Z',
      ended_at: '2026-05-03T10:00:03Z',
      model: 'claude-opus-4-20250514',
      provider: 'anthropic',
      tokens_input: 50,
      tokens_output: 8500,
      thinking_tokens: 8000,
      cost_usd: 0.6385,
    },
    {
      id: thinkingId,
      trace_id: traceId,
      parent_span_id: parentId,
      name: 'thinking',
      type: 'llm',
      started_at: '2026-05-03T10:00:00.1Z',
      ended_at: '2026-05-03T10:00:02.5Z',
      model: 'claude-opus-4-20250514',
      provider: 'anthropic',
      span_subtype: 'thinking',
      thinking_tokens: 8000,
      cost_usd: 0.6,
      input: JSON.stringify({ thinking: reasoningText }),
    },
    {
      id: responseId,
      trace_id: traceId,
      parent_span_id: parentId,
      name: 'response',
      type: 'llm',
      started_at: '2026-05-03T10:00:02.5Z',
      ended_at: '2026-05-03T10:00:03Z',
      model: 'claude-opus-4-20250514',
      provider: 'anthropic',
      span_subtype: 'response',
      tokens_output: 500,
      cost_usd: 0.0375,
      output: JSON.stringify({ text: '2+2 equals 4.' }),
    },
  ]);

  await page.goto(`/traces/${traceId}`);

  // Span timeline shows the thinking row
  const thinkingBadge = page.getByTestId('thinking-badge');
  await expect(thinkingBadge).toBeVisible({ timeout: 10_000 });

  // Brain emoji renders for the thinking row
  await expect(page.getByText('🧠')).toBeVisible();

  // Token count appears in violet
  const tokens = page.getByTestId('thinking-tokens');
  await expect(tokens).toContainText('8000');
  await expect(tokens).toContainText('thinking tok');

  // Trace-level thinking-vs-response breakdown is rendered
  const breakdown = page.getByTestId('thinking-breakdown');
  await expect(breakdown).toBeVisible();
  await expect(breakdown).toContainText('Thinking tokens');
  await expect(breakdown).toContainText('Response tokens');
  await expect(breakdown).toContainText('8,000');
  await expect(breakdown).toContainText('500');

  // Thinking row is collapsed by default — clicking expands the
  // reasoning panel
  await expect(page.getByText(reasoningText)).not.toBeVisible();
  await page.getByRole('button', { name: /thinking/i }).first().click();
  await expect(page.getByText(reasoningText)).toBeVisible();
});
