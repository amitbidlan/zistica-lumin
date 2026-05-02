'use client';

import Link from 'next/link';
import useSWR from 'swr';
import {
  Span,
  Trace,
  fetcher,
  formatCost,
  formatDuration,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import SpanTimeline from './SpanTimeline';

export default function TraceDetail({ id }: { id: string }) {
  const traceQ = useSWR<Trace>(`/v1/traces/${id}`, fetcher);
  const spansQ = useSWR<Span[]>(`/v1/traces/${id}/spans`, fetcher);

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
      <div>
        <Link
          href="/traces"
          className="text-[var(--muted)] text-xs hover:text-[var(--foreground)]"
        >
          ← All traces
        </Link>
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
