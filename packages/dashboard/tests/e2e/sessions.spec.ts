/**
 * Browser-level E2E for the Sessions feature.
 *
 * Posts traces with shared session_id values directly to the API,
 * then exercises the /sessions list and /sessions/[id] detail
 * pages in real Chromium.
 */
import { test, expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const API_BASE = process.env.E2E_API_URL ?? 'http://localhost:8000';

function nowIso(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString();
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

async function postTurn(opts: {
  sessionId: string;
  name: string;
  input?: string;
  startedMsOffset?: number;
}): Promise<string> {
  const id = randomUUID();
  await postSpan([
    {
      id, trace_id: id,
      name: opts.name,
      input: opts.input ? JSON.stringify({ args: [opts.input] }) : undefined,
      started_at: nowIso(opts.startedMsOffset ?? 0),
      ended_at: nowIso((opts.startedMsOffset ?? 0) + 50),
      session_id: opts.sessionId,
    },
  ]);
  return id;
}

// ---------- header navigation ----------

test('header has a Sessions link that navigates correctly', async ({ page }) => {
  await page.goto('/traces');
  await page.getByRole('link', { name: 'Sessions' }).first().click();
  await expect(page).toHaveURL(/\/sessions$/);
  await expect(page.getByRole('heading', { name: 'Sessions' })).toBeVisible();
});

// ---------- session list ----------

test('session list groups traces by session_id', async ({ page }) => {
  const sessionId = `e2e-list-${randomUUID()}`;
  await postTurn({ sessionId, name: 'turn 1', input: 'Book me a flight' });
  await postTurn({ sessionId, name: 'turn 2', input: 'Business class', startedMsOffset: 100 });
  await postTurn({ sessionId, name: 'turn 3', input: 'Add hotel', startedMsOffset: 200 });

  await page.goto('/sessions');

  // The session row appears with id and a turn count of 3
  const row = page.locator(`a:has-text("${sessionId}")`).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row).toContainText('3'); // turns column
});

test('session list excludes traces without session_id', async ({ page }) => {
  // Post a trace WITHOUT session_id
  const id = randomUUID();
  await postSpan([
    {
      id, trace_id: id, name: 'standalone',
      started_at: nowIso(), ended_at: nowIso(50),
    },
  ]);

  // Post another trace WITH a session_id (so we can verify the page works at all)
  const sessionId = `e2e-excl-${randomUUID()}`;
  await postTurn({ sessionId, name: 'in-session' });

  await page.goto('/sessions');
  await expect(page.locator(`a:has-text("${sessionId}")`).first()).toBeVisible();
  // The standalone trace's id should NOT appear as a session
  await expect(page.locator(`a:has-text("${id}")`)).toHaveCount(0);
});

// ---------- session detail / conversation timeline ----------

test('session detail renders turns in chronological order with click-to-expand', async ({ page }) => {
  const sessionId = `e2e-detail-${randomUUID()}`;
  await postTurn({ sessionId, name: 'turn 1', input: 'Book me a flight to Tokyo' });
  await postTurn({ sessionId, name: 'turn 2', input: 'Make it business class', startedMsOffset: 100 });
  await postTurn({ sessionId, name: 'turn 3', input: 'Add hotel for 3 nights', startedMsOffset: 200 });

  await page.goto(`/sessions/${encodeURIComponent(sessionId)}`);

  // Header with the session id
  await expect(page.getByText(sessionId).first()).toBeVisible();

  // Conversation timeline shows 3 turns
  await expect(page.getByText(/Conversation timeline \(3 turns\)/i)).toBeVisible();

  // Each turn label appears (use exact match — getByText is case-
  // insensitive by default, which would also match the lowercase trace
  // names "turn 1" / "turn 2" / "turn 3" we posted as the trace.name).
  await expect(page.getByText('Turn 1', { exact: true })).toBeVisible();
  await expect(page.getByText('Turn 2', { exact: true })).toBeVisible();
  await expect(page.getByText('Turn 3', { exact: true })).toBeVisible();

  // Order: Turn 1 must come before Turn 3 in the DOM
  const turn1Box = await page
    .getByText('Turn 1', { exact: true })
    .first()
    .boundingBox();
  const turn3Box = await page
    .getByText('Turn 3', { exact: true })
    .first()
    .boundingBox();
  expect(turn1Box).not.toBeNull();
  expect(turn3Box).not.toBeNull();
  if (turn1Box && turn3Box) {
    expect(turn1Box.y).toBeLessThan(turn3Box.y);
  }

  // Click the first turn — its input should expand into view
  await page.getByText('Turn 1', { exact: true }).first().click();
  await expect(page.getByText('Book me a flight to Tokyo')).toBeVisible({
    timeout: 5_000,
  });

  // The expanded turn shows its span timeline header
  await expect(page.getByText(/Span timeline/i).first()).toBeVisible();
});

test('404 detail for a non-existent session', async ({ page }) => {
  const bogus = `does-not-exist-${randomUUID()}`;
  await page.goto(`/sessions/${bogus}`);
  await expect(page.getByText(/not found/i)).toBeVisible({ timeout: 10_000 });
});

// ---------- live: WS push refreshes the session list ----------

test('a new turn pushed via WS updates the session list trace_count', async ({ page }) => {
  const sessionId = `e2e-live-${randomUUID()}`;

  // Seed with one turn so the session exists when we land on the page
  await postTurn({ sessionId, name: 'first turn' });

  await page.goto('/sessions');
  const rowSelector = `a:has-text("${sessionId}")`;
  await expect(page.locator(rowSelector).first()).toBeVisible({ timeout: 10_000 });
  // Initially 1 turn
  await expect(page.locator(rowSelector).first()).toContainText('1');

  // Add two more turns — the WS push should refresh the count to 3
  await postTurn({ sessionId, name: 'second turn', startedMsOffset: 100 });
  await postTurn({ sessionId, name: 'third turn', startedMsOffset: 200 });

  // Wait up to 6s for the WS push to land the refresh
  await expect(page.locator(rowSelector).first()).toContainText('3', {
    timeout: 6_000,
  });
});
