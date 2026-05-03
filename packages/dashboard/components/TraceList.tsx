'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  Trace,
  deriveStatus,
  fetcher,
  formatCost,
  formatDuration,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import { useTraceStream, WSMessage } from '@/lib/websocket';

const COLS = 'grid grid-cols-[1fr_180px_100px_120px_100px_80px] gap-4 px-3 py-2';
const PAGE_SIZES = [15, 25, 50, 100];
const PAGE_SIZE_DEFAULT = 15;
const PAGE_SIZE_STORAGE_KEY = 'synaptic.pageSize';

function readStoredPageSize(): number {
  if (typeof window === 'undefined') return PAGE_SIZE_DEFAULT;
  try {
    const raw = window.localStorage.getItem(PAGE_SIZE_STORAGE_KEY);
    const n = raw ? Number(raw) : NaN;
    return PAGE_SIZES.includes(n) ? n : PAGE_SIZE_DEFAULT;
  } catch {
    // localStorage can throw in Safari private mode etc.
    return PAGE_SIZE_DEFAULT;
  }
}

export default function TraceList() {
  const [page, setPage] = useState(0);
  // Start with the SSR-safe default; sync from localStorage after mount to
  // avoid a hydration mismatch.
  const [pageSize, setPageSizeState] = useState(PAGE_SIZE_DEFAULT);

  useEffect(() => {
    const stored = readStoredPageSize();
    if (stored !== PAGE_SIZE_DEFAULT) setPageSizeState(stored);
  }, []);

  const setPageSize = (n: number) => {
    setPageSizeState(n);
    setPage(0);
    try {
      window.localStorage.setItem(PAGE_SIZE_STORAGE_KEY, String(n));
    } catch {
      /* swallow — non-critical */
    }
  };

  // Fetch one extra row beyond the page so we can detect "is there a next
  // page" without making a separate count query.
  const offset = page * pageSize;
  const swrKey = `/v1/traces?limit=${pageSize + 1}&offset=${offset}`;
  const { mutate } = useSWRConfig();

  // Real-time updates via WebSocket. When connected, we don't need to
  // poll. When disconnected, fall back to 5-second polling on page 0.
  const wsState = useTraceStream(
    useCallback(
      (msg: WSMessage) => {
        // Only apply trace inserts on page 0 — on deeper pages, prepending
        // would shift the offset window and feel jittery.
        if (msg.type !== 'new_trace' || page !== 0) return;
        mutate<Trace[]>(
          swrKey,
          (current) => {
            if (!current) return [msg.trace];
            if (current.some((t) => t.id === msg.trace.id)) return current;
            // Keep the +1 probe semantic: page contains pageSize+1 entries
            return [msg.trace, ...current].slice(0, pageSize + 1);
          },
          { revalidate: false },
        );
      },
      [page, pageSize, swrKey, mutate],
    ),
  );

  const { data, error, isLoading } = useSWR<Trace[]>(swrKey, fetcher, {
    // Polling is the FALLBACK. Only run it when WS is not connected and
    // the user is on page 0 (deeper pages don't auto-update either way).
    refreshInterval: page === 0 && wsState !== 'connected' ? 5000 : 0,
    // Don't flicker "Loading…" when navigating between pages.
    keepPreviousData: true,
  });

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load traces: {String(error.message ?? error)}
        <div className="text-[var(--muted)] text-xs mt-2">
          Make sure the API is running at localhost:8000.
        </div>
      </div>
    );
  }

  if (isLoading || !data) {
    return <div className="text-[var(--muted)]">Loading…</div>;
  }

  const hasMore = data.length > pageSize;
  const visible = data.slice(0, pageSize);

  // Empty state — only show on page 0
  if (visible.length === 0 && page === 0) {
    return (
      <div className="text-[var(--muted)]">
        No traces yet. Run an agent with{' '}
        <code className="font-mono">@synaptic.trace</code> to see them here.
      </div>
    );
  }

  const startIdx = offset + 1;
  const endIdx = offset + visible.length;

  return (
    <div>
      <div className="border border-[var(--border)] rounded">
        <div
          className={`${COLS} text-xs uppercase tracking-wider text-[var(--muted)] border-b border-[var(--border)]`}
        >
          <div>Name</div>
          <div>Started</div>
          <div>Duration</div>
          <div>Cost</div>
          <div>Quality</div>
          <div>Status</div>
        </div>
        {visible.length === 0 ? (
          <div className="px-3 py-8 text-center text-[var(--muted)]">
            No traces on this page.{' '}
            <button
              onClick={() => setPage(0)}
              className="underline hover:text-[var(--foreground)]"
            >
              Back to first page
            </button>
          </div>
        ) : (
          visible.map((t) => (
            <Link
              key={t.id}
              href={`/traces/${t.id}`}
              className={`${COLS} border-b border-[var(--border)] last:border-b-0 hover:bg-[#111418] transition-colors`}
            >
              <div className="font-mono truncate">{t.name ?? t.id}</div>
              <div className="text-[var(--muted)]">
                {formatStartedAt(t.started_at)}
              </div>
              <div>{formatDuration(t.duration_ms)}</div>
              <div>{formatCost(t.total_cost_usd)}</div>
              <div>{formatScore(t.quality_score)}</div>
              <div>
                <StatusBadge value={deriveStatus(t)} />
              </div>
            </Link>
          ))
        )}
      </div>

      {/* Pagination footer */}
      <div className="mt-4 flex items-center justify-between gap-3 flex-wrap text-xs">
        <div className="flex items-center gap-3 text-[var(--muted)]">
          <span>
            {visible.length === 0
              ? 'No results'
              : `${startIdx}–${endIdx}`}
            {' · page '}
            {page + 1}
            {page === 0 && (
              <span
                className="ml-2 inline-flex items-center gap-1"
                title={
                  wsState === 'connected'
                    ? 'WebSocket connected — real-time updates'
                    : 'WebSocket unavailable — polling every 5s'
                }
              >
                <span
                  className={
                    wsState === 'connected'
                      ? 'inline-block h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse'
                      : 'inline-block h-1.5 w-1.5 rounded-full bg-slate-500'
                  }
                />
                {wsState === 'connected' ? 'live' : 'polling'}
              </span>
            )}
          </span>
          <span className="opacity-30">|</span>
          <label className="flex items-center gap-1.5">
            <span>Per page:</span>
            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              className="bg-transparent border border-[var(--border)] rounded px-1.5 py-0.5 text-[var(--foreground)] focus:outline-none focus:border-[var(--accent)]"
            >
              {PAGE_SIZES.map((n) => (
                <option key={n} value={n} className="bg-[var(--background)]">
                  {n}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="flex items-center gap-2">
          {page > 0 && (
            <button
              onClick={() => setPage(0)}
              className="px-2 py-1 text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
              title="Jump to first page"
            >
              ⇤ First
            </button>
          )}
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 border border-[var(--border)] rounded hover:bg-[#111418] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            ← Previous
          </button>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={!hasMore}
            className="px-3 py-1 border border-[var(--border)] rounded hover:bg-[#111418] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  );
}

function StatusBadge({ value }: { value: 'ok' | 'running' | 'error' }) {
  const styles: Record<typeof value, string> = {
    ok: 'bg-emerald-900/40 text-emerald-300 border-emerald-700',
    running: 'bg-amber-900/30 text-amber-300 border-amber-700',
    error: 'bg-red-900/40 text-red-300 border-red-700',
  };
  return (
    <span
      className={`inline-block text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded ${styles[value]}`}
    >
      {value}
    </span>
  );
}
