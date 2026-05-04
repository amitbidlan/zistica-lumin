'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  Session,
  fetcher,
  formatCost,
  formatDuration,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import { useTraceStream, WSMessage } from '@/lib/websocket';

const COLS =
  'grid grid-cols-[1fr_180px_100px_120px_100px_80px] gap-4 px-3 py-2';
const PAGE_SIZE_DEFAULT = 15;
const PAGE_SIZES = [15, 25, 50, 100];
const PAGE_SIZE_STORAGE_KEY = 'lumin.pageSize';

function readStoredPageSize(): number {
  if (typeof window === 'undefined') return PAGE_SIZE_DEFAULT;
  try {
    const raw = window.localStorage.getItem(PAGE_SIZE_STORAGE_KEY);
    const n = raw ? Number(raw) : NaN;
    return PAGE_SIZES.includes(n) ? n : PAGE_SIZE_DEFAULT;
  } catch {
    return PAGE_SIZE_DEFAULT;
  }
}

export default function SessionList() {
  const [page, setPage] = useState(0);
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
      /* swallow */
    }
  };

  const offset = page * pageSize;
  const swrKey = `/v1/sessions?limit=${pageSize + 1}&offset=${offset}`;
  const { mutate } = useSWRConfig();

  // Listen for new traces — when one arrives with a session_id, the
  // session aggregations (trace_count, totals, last_seen) might have
  // changed. Trigger a revalidation to refresh the session list.
  const wsState = useTraceStream((msg: WSMessage) => {
    if (msg.type === 'new_trace' && msg.trace.session_id && page === 0) {
      mutate(swrKey);
    }
  });

  const { data, error, isLoading } = useSWR<Session[]>(swrKey, fetcher, {
    refreshInterval: page === 0 && wsState !== 'connected' ? 5000 : 0,
    keepPreviousData: true,
  });

  // Catch-up revalidation on WS reconnect
  const wasDisconnected = useRef(false);
  useEffect(() => {
    if (wsState === 'disconnected') {
      wasDisconnected.current = true;
    } else if (wsState === 'connected' && wasDisconnected.current) {
      mutate(swrKey);
      wasDisconnected.current = false;
    }
  }, [wsState, swrKey, mutate]);

  if (error) {
    return (
      <div className="text-red-400">
        Failed to load sessions: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="text-[var(--muted)]">Loading…</div>;
  }

  const hasMore = data.length > pageSize;
  const visible = data.slice(0, pageSize);

  if (visible.length === 0 && page === 0) {
    return (
      <div className="text-[var(--muted)]">
        No sessions yet. Wrap your agent calls in{' '}
        <code className="font-mono">lumin.session(name=&quot;…&quot;)</code> to
        group multi-turn conversations together.
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
          <div>Session</div>
          <div>Last activity</div>
          <div>Turns</div>
          <div>Total cost</div>
          <div>Avg quality</div>
          <div>Tokens</div>
        </div>
        {visible.length === 0 ? (
          <div className="px-3 py-8 text-center text-[var(--muted)]">
            No sessions on this page.{' '}
            <button
              onClick={() => setPage(0)}
              className="underline hover:text-[var(--foreground)]"
            >
              Back to first page
            </button>
          </div>
        ) : (
          visible.map((s) => (
            <Link
              key={s.session_id}
              href={`/sessions/${encodeURIComponent(s.session_id)}`}
              className={`${COLS} border-b border-[var(--border)] last:border-b-0 hover:bg-[#111418] transition-colors`}
            >
              <div className="font-mono truncate">{s.session_id}</div>
              <div className="text-[var(--muted)]">
                {formatStartedAt(s.last_seen)}
              </div>
              <div className="font-mono">{s.trace_count}</div>
              <div>{formatCost(s.total_cost_usd)}</div>
              <div>{formatScore(s.quality_score)}</div>
              <div className="text-[var(--muted)] font-mono text-xs">
                {s.total_tokens.toLocaleString()}
              </div>
            </Link>
          ))
        )}
      </div>

      <div className="mt-4 flex items-center justify-between gap-3 flex-wrap text-xs">
        <div className="flex items-center gap-3 text-[var(--muted)]">
          <span>
            {visible.length === 0 ? 'No results' : `${startIdx}–${endIdx}`}
            {' · page '}
            {page + 1}
            {page === 0 && (
              <span
                className="ml-2 inline-flex items-center gap-1"
                title={
                  wsState === 'connected'
                    ? 'WebSocket connected — sessions update live'
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
