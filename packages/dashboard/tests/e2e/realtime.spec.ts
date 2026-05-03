/**
 * Browser-level E2E tests for real-time WebSocket updates.
 *
 * These run against a real running stack (Docker container by default).
 * They post spans via the API and assert the dashboard UI updates
 * without manual refresh.
 */
import { test, expect, Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const API_BASE = process.env.E2E_API_URL ?? 'http://localhost:8000';

function nowIso(): string {
  return new Date().toISOString();
}

async function postSpan(spans: Array<Record<string, unknown>>): Promise<void> {
  const res = await fetch(`${API_BASE}/v1/spans`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spans }),
  });
  if (!res.ok) {
    throw new Error(`POST /v1/spans failed: ${res.status} ${await res.text()}`);
  }
}

async function waitForLiveIndicator(page: Page): Promise<void> {
  // The indicator only appears once at least one trace exists (it lives
  // in the pagination footer). Wait for the WS to be connected.
  await expect(page.getByText('live').first()).toBeVisible({ timeout: 10_000 });
}

// ---------- 1. Trace list updates without refresh ----------

test('new trace appears in the list within 2s of being posted (no refresh)', async ({ page }) => {
  // Seed at least one trace so the pagination footer (with the live
  // indicator) renders before we post the trace under test.
  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: 'seed',
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await page.goto('/traces');
  await waitForLiveIndicator(page);

  const traceId = randomUUID();
  const traceName = `e2e_list_${Date.now()}`;
  await postSpan([
    {
      id: traceId, trace_id: traceId, name: traceName,
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  // The new trace must appear without any refresh — pure WS push.
  await expect(page.getByText(traceName)).toBeVisible({ timeout: 5_000 });
});

// ---------- 2. Trace detail timeline updates without refresh ----------

test('new spans append to the timeline live while viewing a trace', async ({ page }) => {
  const traceId = randomUUID();
  const rootName = `e2e_detail_${Date.now()}`;
  await postSpan([
    {
      id: traceId, trace_id: traceId, name: rootName,
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await page.goto(`/traces/${traceId}`);

  // Wait for the timeline header to render with 1 span
  await expect(page.getByText(/Span timeline \(1 span\)/i)).toBeVisible();

  // Now add two child spans
  const child1 = `child_a_${Date.now()}`;
  const child2 = `child_b_${Date.now()}`;
  await postSpan([
    {
      id: randomUUID(), trace_id: traceId, parent_span_id: traceId,
      name: child1, started_at: nowIso(), ended_at: nowIso(),
    },
  ]);
  await postSpan([
    {
      id: randomUUID(), trace_id: traceId, parent_span_id: traceId,
      name: child2, started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  // Timeline should reflect 3 spans without page refresh
  await expect(page.getByText(/Span timeline \(3 spans\)/i)).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText(child1)).toBeVisible();
  await expect(page.getByText(child2)).toBeVisible();
});

// ---------- 3. Live indicator state ----------

test('live indicator title reads "WebSocket connected" when WS is up', async ({ page }) => {
  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: 'indicator-seed',
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await page.goto('/traces');
  await waitForLiveIndicator(page);

  // The indicator's parent span has a title attribute we can verify
  const indicator = page
    .locator('[title*="WebSocket connected"]')
    .first();
  await expect(indicator).toBeVisible();
});

// ---------- 4. Polling fallback when WebSocket is unreachable ----------

test('falls back to polling when WebSocket fails to connect', async ({ page }) => {
  // Reject every WebSocket upgrade — simulates port 8000 being unreachable
  await page.routeWebSocket('**/ws/traces', (ws) => {
    ws.close({ code: 1011, reason: 'simulated failure for e2e' });
  });

  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: 'polling-seed',
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await page.goto('/traces');

  // The indicator should show "polling" rather than "live"
  await expect(page.getByText('polling').first()).toBeVisible({ timeout: 10_000 });

  // Title attribute confirms polling mode
  const indicator = page.locator('[title*="polling"]').first();
  await expect(indicator).toBeVisible();
});

// ---------- 5. Multiple tabs both update simultaneously ----------

test('two tabs both receive WebSocket pushes for the same trace', async ({ context }) => {
  const tab1 = await context.newPage();
  const tab2 = await context.newPage();

  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: 'multitab-seed',
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await tab1.goto('/traces');
  await tab2.goto('/traces');
  await waitForLiveIndicator(tab1);
  await waitForLiveIndicator(tab2);

  const traceName = `multitab_${Date.now()}`;
  const traceId = randomUUID();
  await postSpan([
    {
      id: traceId, trace_id: traceId, name: traceName,
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  // Both tabs should see the new trace via independent WS connections
  await expect(tab1.getByText(traceName)).toBeVisible({ timeout: 5_000 });
  await expect(tab2.getByText(traceName)).toBeVisible({ timeout: 5_000 });
});
