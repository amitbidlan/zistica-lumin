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

  // Wait for the spans tab to render with 1 span. (The trace detail page
  // grew tabs in the policy-engine PR — what was a section header
  // "Span timeline (1 span)" is now the "Spans (1)" tab label.)
  await expect(page.getByRole('button', { name: /Spans \(1\)/ })).toBeVisible();

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

  // Spans tab should reflect 3 spans without page refresh
  await expect(page.getByRole('button', { name: /Spans \(3\)/ })).toBeVisible({ timeout: 5_000 });
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

// ---------- 4. Polling fallback indicator ----------

test('indicator flips to "polling" when WebSocket fails to connect', async ({ page }) => {
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

// ---------- 4b. Polling ACTUALLY fetches new data (not just UI state) ----------

test('polling fallback actually retrieves new traces (not just label)', async ({ page }) => {
  // Block WS so the dashboard is forced into polling mode
  await page.routeWebSocket('**/ws/traces', (ws) => {
    ws.close({ code: 1011, reason: 'forcing polling fallback' });
  });

  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: 'polling-real-seed',
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await page.goto('/traces');
  await expect(page.getByText('polling').first()).toBeVisible({ timeout: 10_000 });

  // Now post a NEW trace — there is no WS to push it. The dashboard
  // must catch up via the 5-second polling cycle. Allow up to ~7s
  // (one polling tick + buffer).
  const traceName = `polling_data_${Date.now()}`;
  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: traceName,
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await expect(page.getByText(traceName)).toBeVisible({ timeout: 7_000 });
});

// ---------- 5. Mid-session reconnect: WS recovers WITHOUT page reload ----------

test('mid-session reconnect: WS comes back, catch-up surfaces missed trace', async ({ page }) => {
  // Default test timeout is 30s; this scenario waits through exponential
  // backoff (worst case ~31s before the cap). Bump for this test only.
  test.setTimeout(60_000);

  // Stateful handler — flip the flag mid-test to allow reconnect.
  // Closure is read fresh on each route invocation, so once flipped to
  // false, the next reconnect attempt passes through to the real server.
  let blockWs = true;
  await page.routeWebSocket('**/ws/traces', async (ws) => {
    if (blockWs) {
      ws.close({ code: 1011, reason: 'simulated outage' });
      return;
    }
    await ws.connectToServer();
  });

  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: 'midrec-seed',
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  await page.goto('/traces');
  await expect(page.getByText('polling').first()).toBeVisible({ timeout: 10_000 });

  // Post a trace WHILE the WebSocket is unreachable. The new_trace
  // push is broadcast but our subscriber misses it.
  const missedName = `missed_${Date.now()}`;
  await postSpan([
    {
      id: randomUUID(), trace_id: randomUUID(), name: missedName,
      started_at: nowIso(), ended_at: nowIso(),
    },
  ]);

  // Allow future reconnect attempts to pass through. NO page reload:
  // this proves the dashboard's mid-session reconnect + catch-up
  // logic works (not just the trivial fresh-load case).
  blockWs = false;

  // Wait for the hook to retry and succeed. Worst case: ~31s of
  // exponential backoff.
  await expect(page.getByText('live').first()).toBeVisible({ timeout: 35_000 });

  // The missed trace must surface — either via the catch-up `mutate`
  // that fires on disconnected→connected transition, or via polling
  // having already picked it up. Both code paths are valid.
  await expect(page.getByText(missedName)).toBeVisible({ timeout: 5_000 });
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
