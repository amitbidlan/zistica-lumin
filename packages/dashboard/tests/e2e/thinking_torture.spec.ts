/**
 * Dashboard torture tests for thinking-block visualization.
 *
 * These probe edge cases that real production traffic could hit:
 *  - Multiple thinking spans in one trace (multi-turn reasoning)
 *  - Empty reasoning text
 *  - Reasoning > 100KB (will it lock up the page?)
 *  - Mixed thinking + tool + response children
 *  - A trace with only a thinking span (no response yet, mid-stream)
 *  - Subtype values the dashboard doesn't recognize (future-proofing)
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

const BASE_TIME = '2026-05-03T10:00:00Z';

function nowIso(offsetMs = 0): string {
  return new Date(Date.parse(BASE_TIME) + offsetMs).toISOString();
}

// ---------- 1. Many thinking spans in one trace ----------

test('a trace with five separate thinking spans shows all of them', async ({ page }) => {
  const traceId = `torture-multi-${randomUUID()}`;
  const parentId = `p-${randomUUID()}`;
  const spans: Array<Record<string, unknown>> = [
    {
      id: parentId, trace_id: traceId,
      name: 'multi_turn_chain', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(10000),
      thinking_tokens: 5000,
    },
  ];
  for (let i = 0; i < 5; i++) {
    spans.push({
      id: `t-${i}-${randomUUID()}`, trace_id: traceId, parent_span_id: parentId,
      name: 'thinking', type: 'llm',
      started_at: nowIso(i * 1000), ended_at: nowIso(i * 1000 + 500),
      span_subtype: 'thinking',
      thinking_tokens: 1000,
      cost_usd: 0.075,
      input: JSON.stringify({ thinking: `Reasoning step ${i + 1} — derive the next claim.` }),
    });
  }
  await postSpans(spans);
  await page.goto(`/traces/${traceId}`);

  // All five thinking badges appear
  await expect(page.getByTestId('thinking-badge')).toHaveCount(5, { timeout: 10_000 });
  // Header breakdown should sum the five thinking spans (5000 tokens, $0.375)
  const breakdown = page.getByTestId('thinking-breakdown');
  await expect(breakdown).toContainText('5,000');
  await expect(breakdown).toContainText('$0.3750');
});

// ---------- 2. Empty reasoning content ----------

test('thinking span with empty reasoning text shows (empty) without crashing', async ({ page }) => {
  const traceId = `torture-empty-${randomUUID()}`;
  const parentId = `p-${randomUUID()}`;
  await postSpans([
    {
      id: parentId, trace_id: traceId,
      name: 'claude_call', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(1000),
    },
    {
      id: `t-${randomUUID()}`, trace_id: traceId, parent_span_id: parentId,
      name: 'thinking', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(500),
      span_subtype: 'thinking',
      thinking_tokens: 0,
      input: JSON.stringify({ thinking: '' }),
    },
  ]);
  await page.goto(`/traces/${traceId}`);
  const badge = page.getByTestId('thinking-badge');
  await expect(badge).toBeVisible({ timeout: 10_000 });

  // Click to expand and verify it shows "(empty)" instead of crashing
  await page.getByRole('button').filter({ has: badge }).click();
  await expect(page.getByText('(empty)').first()).toBeVisible();
});

// ---------- 3. 100KB reasoning text doesn't freeze the browser ----------

test('100KB reasoning text renders without hanging the page', async ({ page }) => {
  const traceId = `torture-huge-${randomUUID()}`;
  const parentId = `p-${randomUUID()}`;
  const huge = 'REASONING-TOKEN '.repeat(6500); // ~100KB
  await postSpans([
    {
      id: parentId, trace_id: traceId,
      name: 'claude_call', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(1000),
    },
    {
      id: `t-${randomUUID()}`, trace_id: traceId, parent_span_id: parentId,
      name: 'thinking', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(500),
      span_subtype: 'thinking',
      thinking_tokens: 25000,
      input: JSON.stringify({ thinking: huge }),
    },
  ]);
  const start = Date.now();
  await page.goto(`/traces/${traceId}`);
  await expect(page.getByTestId('thinking-badge')).toBeVisible({ timeout: 10_000 });
  const elapsed = Date.now() - start;
  expect(elapsed).toBeLessThan(8000); // page must hydrate within 8s
});

// ---------- 4. Mixed thinking + tool + response coexist correctly ----------

test('thinking, tool, and response children all render with correct badges', async ({ page }) => {
  const traceId = `torture-mixed-${randomUUID()}`;
  const parent = `p-${randomUUID()}`;
  await postSpans([
    {
      id: parent, trace_id: traceId,
      name: 'agent_with_tools', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(5000),
    },
    {
      id: `t-${randomUUID()}`, trace_id: traceId, parent_span_id: parent,
      name: 'thinking', type: 'llm',
      started_at: nowIso(100), ended_at: nowIso(2000),
      span_subtype: 'thinking', thinking_tokens: 800,
      input: JSON.stringify({ thinking: 'Should I call get_weather?' }),
    },
    {
      id: `tool-${randomUUID()}`, trace_id: traceId, parent_span_id: parent,
      name: 'get_weather', type: 'tool', tool_name: 'get_weather',
      started_at: nowIso(2000), ended_at: nowIso(2500),
      input: '{"city":"SF"}',
      output: '{"temp":62}',
    },
    {
      id: `r-${randomUUID()}`, trace_id: traceId, parent_span_id: parent,
      name: 'response', type: 'llm',
      started_at: nowIso(3000), ended_at: nowIso(5000),
      span_subtype: 'response',
      tokens_output: 200,
      output: JSON.stringify({ text: 'It is 62°F in San Francisco.' }),
    },
  ]);
  await page.goto(`/traces/${traceId}`);
  await expect(page.getByTestId('thinking-badge')).toBeVisible({ timeout: 10_000 });
  // Tool span renders its name (no subtype badge)
  await expect(page.getByText('get_weather').first()).toBeVisible();
  // Cost breakdown shows thinking AND response totals
  const breakdown = page.getByTestId('thinking-breakdown');
  await expect(breakdown).toContainText('Thinking tokens');
  await expect(breakdown).toContainText('Response tokens');
});

// ---------- 5. Mid-stream trace (thinking but no response yet) ----------

test('trace with thinking but no response renders thinking row + breakdown', async ({ page }) => {
  const traceId = `torture-streaming-${randomUUID()}`;
  const parentId = `p-${randomUUID()}`;
  await postSpans([
    {
      id: parentId, trace_id: traceId,
      name: 'claude_call', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(2000),
    },
    {
      id: `t-${randomUUID()}`, trace_id: traceId, parent_span_id: parentId,
      name: 'thinking', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(2000),
      span_subtype: 'thinking', thinking_tokens: 500,
      input: JSON.stringify({ thinking: 'still reasoning…' }),
    },
  ]);
  await page.goto(`/traces/${traceId}`);
  await expect(page.getByTestId('thinking-badge')).toBeVisible({ timeout: 10_000 });
  const breakdown = page.getByTestId('thinking-breakdown');
  await expect(breakdown).toBeVisible();
  await expect(breakdown).toContainText('500');
  // No response — but breakdown still shows
});

// ---------- 6. Unknown subtype value doesn't break SpanRow ----------

test('unknown span_subtype falls through to default rendering without crashing', async ({ page }) => {
  const traceId = `torture-unknown-${randomUUID()}`;
  await postSpans([
    {
      id: `p-${randomUUID()}`, trace_id: traceId,
      name: 'experimental_call', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(1000),
      span_subtype: 'tool_thinking_v2',
    },
  ]);
  await page.goto(`/traces/${traceId}`);
  // Wait for the SWR-driven span timeline to render. The trace detail
  // page now uses a "Spans (N)" tab in place of the old "Span timeline"
  // section header — wait for the tab.
  await expect(page.getByRole('button', { name: /Spans \(\d+\)/ })).toBeVisible({ timeout: 10_000 });
  // The custom subtype produces no thinking badge (only known
  // subtypes get badges) — page rendered without crashing
  await expect(page.getByTestId('thinking-badge')).toHaveCount(0);
});

// ---------- 7. Unicode in reasoning ----------

test('unicode and emoji in reasoning render correctly', async ({ page }) => {
  const traceId = `torture-unicode-${randomUUID()}`;
  const parentId = `p-${randomUUID()}`;
  const reasoning = '推論 → なぜ 2+2=4? 🧠✓ こたえ: ４';
  await postSpans([
    {
      id: parentId, trace_id: traceId,
      name: 'claude_call', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(1000),
    },
    {
      id: `t-${randomUUID()}`, trace_id: traceId, parent_span_id: parentId,
      name: 'thinking', type: 'llm',
      started_at: nowIso(0), ended_at: nowIso(500),
      span_subtype: 'thinking', thinking_tokens: 50,
      input: JSON.stringify({ thinking: reasoning }),
    },
  ]);
  await page.goto(`/traces/${traceId}`);
  const badge = page.getByTestId('thinking-badge');
  await expect(badge).toBeVisible({ timeout: 10_000 });
  await page.getByRole('button').filter({ has: badge }).click();
  await expect(page.getByText('推論')).toBeVisible();
  await expect(page.getByText('🧠✓')).toBeVisible();
});
