'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  Span,
  Trace,
  fetcher,
  formatCost,
  formatDuration,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import { useTraceStream, WSMessage } from '@/lib/websocket';
import SpanTimeline from './SpanTimeline';

export default function TraceDetail({ id }: { id: string }) {
  const traceKey = `/v1/traces/${id}`;
  const spansKey = `/v1/traces/${id}/spans`;
  const { mutate } = useSWRConfig();

  // Subscribe to real-time span events for THIS trace. New spans are
  // appended to the SWR cache, which re-renders SpanTimeline with the
  // new node nested under its parent.
  const wsState = useTraceStream(
    useCallback(
      (msg: WSMessage) => {
        if (msg.type === 'new_span' && msg.trace_id === id) {
          mutate<Span[]>(
            spansKey,
            (current) => {
              if (!current) return [msg.span];
              if (current.some((s) => s.id === msg.span.id)) return current;
              return [...current, msg.span];
            },
            { revalidate: false },
          );
        } else if (msg.type === 'new_trace' && msg.trace.id === id) {
          // Trace metadata might update too (e.g. ended_at when the root
          // span finally lands after orphan children).
          mutate<Trace>(traceKey, msg.trace, { revalidate: false });
        }
      },
      [id, spansKey, traceKey, mutate],
    ),
  );

  // Polling fallback: when WS is down, refresh every 5s so the timeline
  // doesn't go stale. When WS is connected, no polling — pushes are
  // authoritative.
  const refreshInterval = wsState !== 'connected' ? 5000 : 0;
  const traceQ = useSWR<Trace>(traceKey, fetcher, { refreshInterval });
  const spansQ = useSWR<Span[]>(spansKey, fetcher, { refreshInterval });

  // Catch-up revalidation: when WS transitions disconnected → connected,
  // events that arrived during the gap have already been lost from the
  // pushed stream. Force a one-shot revalidation so the cache catches up
  // to whatever the API has, then resume live updates from there.
  const prevWsState = useRef(wsState);
  useEffect(() => {
    if (prevWsState.current !== 'connected' && wsState === 'connected') {
      mutate(traceKey);
      mutate(spansKey);
    }
    prevWsState.current = wsState;
  }, [wsState, traceKey, spansKey, mutate]);

  if (traceQ.error) {
    return (
      <div className="text-red-400">
        Failed to load trace: {String(traceQ.error.message ?? traceQ.error)}
      </div>
    );
  }
  if (!traceQ.data || !spansQ.data) {
    return <div className="text-[var(--muted)]">Loading…</div>;
  }

  const trace = traceQ.data;
  const spans = spansQ.data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link
          href="/traces"
          className="text-[var(--muted)] text-xs hover:text-[var(--foreground)]"
        >
          ← All traces
        </Link>
        <span
          className="inline-flex items-center gap-1 text-xs text-[var(--muted)]"
          title={
            wsState === 'connected'
              ? 'WebSocket connected — new spans appear live'
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
      </div>

      <header className="border border-[var(--border)] rounded p-4">
        <h1 className="font-mono text-base mb-1">{trace.name ?? trace.id}</h1>
        <div className="text-[var(--muted)] text-xs font-mono mb-3">
          {trace.id}
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <Metric label="Started" value={formatStartedAt(trace.started_at)} />
          <Metric label="Duration" value={formatDuration(trace.duration_ms)} />
          <Metric label="Total cost" value={formatCost(trace.total_cost_usd)} />
          <Metric label="Quality" value={formatScore(trace.quality_score)} />
        </div>
      </header>

      <section>
        <h2 className="text-xs uppercase tracking-wider text-[var(--muted)] mb-2">
          Span timeline ({spans.length} {spans.length === 1 ? 'span' : 'spans'})
        </h2>
        {spans.length === 0 ? (
          <div className="text-[var(--muted)] text-sm">
            No spans for this trace.
          </div>
        ) : (
          <SpanTimeline spans={spans} />
        )}
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--muted)]">
        {label}
      </div>
      <div className="font-mono">{value}</div>
    </div>
  );
}
