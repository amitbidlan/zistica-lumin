'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  Session,
  fetcher,
  formatCost,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import { useTraceStream, WSMessage } from '@/lib/websocket';

const COLS =
  'grid grid-cols-[1fr_180px_100px_120px_100px_80px] gap-4 px-4 py-2.5';
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

  const wsState = useTraceStream((msg: WSMessage) => {
    if (msg.type === 'new_trace' && msg.trace.session_id && page === 0) {
      mutate(swrKey);
    }
  });

  const { data, error, isLoading } = useSWR<Session[]>(swrKey, fetcher, {
    refreshInterval: page === 0 && wsState !== 'connected' ? 5000 : 0,
    keepPreviousData: true,
  });

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
      <div className="card p-4 text-rose-400">
        Failed to load sessions: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>;
  }

  const hasMore = data.length > pageSize;
  const visible = data.slice(0, pageSize);

  if (visible.length === 0 && page === 0) {
    return (
      <div className="card p-8 text-center">
        <div className="text-[var(--foreground-soft)] mb-2">No sessions yet.</div>
        <div className="text-[var(--muted)] text-sm">
          Wrap your agent calls in{' '}
          <code className="font-mono">lumin.session(name=&quot;…&quot;)</code> to
          group multi-turn conversations together.
        </div>
      </div>
    );
  }

  const startIdx = offset + 1;
  const endIdx = offset + visible.length;

  return (
    <div className="space-y-4">
      <div className="card overflow-hidden">
        <div
          className={`${COLS} text-[10px] uppercase tracking-wider text-[var(--muted)] border-b border-[var(--border)]`}
        >
          <div>Session</div>
          <div>Last activity</div>
          <div>Turns</div>
          <div>Total cost</div>
          <div>Avg quality</div>
          <div>Tokens</div>
        </div>
        {visible.length === 0 ? (
          <div className="px-4 py-8 text-center text-[var(--muted)]">
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
              className={`${COLS} text-sm border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--background-hover)] transition-colors`}
            >
              <div className="font-mono truncate">{s.session_id}</div>
              <div className="text-[var(--muted)] text-xs">
                {formatStartedAt(s.last_seen)}
              </div>
              <div className="metric-value">{s.trace_count}</div>
              <div className="metric-value">{formatCost(s.total_cost_usd)}</div>
              <div className="metric-value">{formatScore(s.quality_score)}</div>
              <div className="text-[var(--muted)] font-mono text-xs">
                {s.total_tokens.toLocaleString()}
              </div>
            </Link>
          ))
        )}
      </div>

      <PaginationFooter
        startIdx={startIdx}
        endIdx={endIdx}
        page={page}
        pageSize={pageSize}
        hasMore={hasMore}
        wsState={wsState}
        onSetPageSize={setPageSize}
        onSetPage={setPage}
        empty={visible.length === 0}
      />
    </div>
  );
}


function PaginationFooter({
  startIdx,
  endIdx,
  page,
  pageSize,
  hasMore,
  wsState,
  onSetPageSize,
  onSetPage,
  empty,
}: {
  startIdx: number;
  endIdx: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  wsState: 'connecting' | 'connected' | 'disconnected';
  onSetPageSize: (n: number) => void;
  onSetPage: (p: number | ((p: number) => number)) => void;
  empty: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3 flex-wrap text-xs">
      <div className="flex items-center gap-3 text-[var(--muted)]">
        <span>
          {empty ? 'No results' : `${startIdx}–${endIdx}`}
          {' · page '}{page + 1}
          {page === 0 ? (
            <span
              className="ml-2 inline-flex items-center gap-1.5"
              title={
                wsState === 'connected'
                  ? 'WebSocket connected — sessions update live'
                  : 'WebSocket unavailable — polling every 5s'
              }
            >
              <span
                className={
                  wsState === 'connected'
                    ? 'activity-dot active'
                    : 'activity-dot dormant'
                }
                style={{ width: 6, height: 6 }}
              />
              {wsState === 'connected' ? 'live' : 'polling'}
            </span>
          ) : null}
        </span>
        <span className="opacity-30">·</span>
        <label className="flex items-center gap-1.5">
          <span>Per page</span>
          <select
            value={pageSize}
            onChange={(e) => onSetPageSize(Number(e.target.value))}
            className="form-input"
            style={{ padding: '0.125rem 0.375rem', fontSize: 12, width: 'auto' }}
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
        {page > 0 ? (
          <button
            onClick={() => onSetPage(0)}
            className="pill"
            title="Jump to first page"
          >
            ⇤ First
          </button>
        ) : null}
        <button
          onClick={() => onSetPage((p) => Math.max(0, p - 1))}
          disabled={page === 0}
          className="pill disabled:opacity-30 disabled:cursor-not-allowed"
        >
          ← Previous
        </button>
        <button
          onClick={() => onSetPage((p) => p + 1)}
          disabled={!hasMore}
          className="pill disabled:opacity-30 disabled:cursor-not-allowed"
        >
          Next →
        </button>
      </div>
    </div>
  );
}
