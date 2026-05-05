'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
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

const COLS =
  'grid grid-cols-[1fr_180px_100px_120px_100px_80px_110px] gap-4 px-4 py-2.5';
const PAGE_SIZES = [15, 25, 50, 100];
const PAGE_SIZE_DEFAULT = 15;
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

export default function TraceList() {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSizeState] = useState(PAGE_SIZE_DEFAULT);
  const [onlyViolated, setOnlyViolated] = useState(false);

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
  const swrKey = `/v1/traces?limit=${pageSize + 1}&offset=${offset}`;
  const { mutate } = useSWRConfig();

  const wsState = useTraceStream(
    useCallback(
      (msg: WSMessage) => {
        if (msg.type !== 'new_trace' || page !== 0) return;
        mutate<Trace[]>(
          swrKey,
          (current) => {
            if (!current) return [msg.trace];
            if (current.some((t) => t.id === msg.trace.id)) return current;
            return [msg.trace, ...current].slice(0, pageSize + 1);
          },
          { revalidate: false },
        );
      },
      [page, pageSize, swrKey, mutate],
    ),
  );

  const { data, error, isLoading } = useSWR<Trace[]>(swrKey, fetcher, {
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
        Failed to load traces: {String(error.message ?? error)}
        <div className="text-[var(--muted)] text-xs mt-2">
          Make sure the API is running at localhost:8000.
        </div>
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
        <div className="text-[var(--foreground-soft)] mb-2">No traces yet.</div>
        <div className="text-[var(--muted)] text-sm">
          Run an agent with{' '}
          <code className="font-mono">@lumin.trace</code> to see them here.
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
          <div>Name</div>
          <div>Started</div>
          <div>Duration</div>
          <div>Cost</div>
          <div>Quality</div>
          <div>Status</div>
          <div>Violations</div>
        </div>
        {visible.length === 0 ? (
          <div className="px-4 py-8 text-center text-[var(--muted)]">
            No traces on this page.{' '}
            <button
              onClick={() => setPage(0)}
              className="underline hover:text-[var(--foreground)]"
            >
              Back to first page
            </button>
          </div>
        ) : (
          visible
            .filter((t) => !onlyViolated || (t.violation_count ?? 0) > 0)
            .map((t) => (
              <Link
                key={t.id}
                href={`/traces/${t.id}`}
                className={`${COLS} text-sm border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--background-hover)] transition-colors`}
              >
                <div className="font-mono truncate">{t.name ?? t.id}</div>
                <div className="text-[var(--muted)] text-xs">
                  {formatStartedAt(t.started_at)}
                </div>
                <div className="metric-value">{formatDuration(t.duration_ms)}</div>
                <div className="metric-value">{formatCost(t.total_cost_usd)}</div>
                <div className="metric-value">{formatScore(t.quality_score)}</div>
                <div>
                  <StatusBadge value={deriveStatus(t)} />
                </div>
                <div>
                  <ViolationBadge count={t.violation_count ?? 0} />
                </div>
              </Link>
            ))
        )}
      </div>

      {/* Pagination footer + filters */}
      <div className="flex items-center justify-between gap-3 flex-wrap text-xs">
        <div className="flex items-center gap-3 text-[var(--muted)]">
          <span>
            {visible.length === 0 ? 'No results' : `${startIdx}–${endIdx}`}
            {' · page '}{page + 1}
            {page === 0 ? (
              <span
                className="ml-2 inline-flex items-center gap-1.5"
                title={
                  wsState === 'connected'
                    ? 'WebSocket connected — real-time updates'
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
          <label className="flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={onlyViolated}
              onChange={(e) => setOnlyViolated(e.target.checked)}
              className="form-checkbox"
            />
            <span>Only violated</span>
          </label>
          <span className="opacity-30">·</span>
          <label className="flex items-center gap-1.5">
            <span>Per page</span>
            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
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
            <button onClick={() => setPage(0)} className="pill" title="Jump to first page">
              ⇤ First
            </button>
          ) : null}
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="pill disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ← Previous
          </button>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={!hasMore}
            className="pill disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  );
}


function ViolationBadge({ count }: { count: number }) {
  if (!count) {
    return <span className="text-[var(--muted)] text-xs">—</span>;
  }
  return (
    <span
      className="badge badge-rose"
      title={`${count} policy violation${count === 1 ? '' : 's'}`}
    >
      {count} violation{count === 1 ? '' : 's'}
    </span>
  );
}


function StatusBadge({ value }: { value: 'ok' | 'running' | 'error' }) {
  const cls = (
    value === 'ok'      ? 'badge badge-emerald' :
    value === 'running' ? 'badge badge-amber' :
                          'badge badge-rose'
  );
  return <span className={cls}>{value}</span>;
}
