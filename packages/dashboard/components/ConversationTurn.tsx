'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';
import {
  Span,
  Trace,
  deriveStatus,
  fetcher,
  formatCost,
  formatDuration,
} from '@/lib/api';
import SpanTimeline from './SpanTimeline';

const STATUS_STYLES: Record<string, string> = {
  ok: 'text-emerald-300 border-emerald-700 bg-emerald-900/30',
  error: 'text-red-300 border-red-700 bg-red-900/30',
  running: 'text-amber-300 border-amber-700 bg-amber-900/30',
};

/** A single turn in a conversation — one trace, expandable to its span tree.
 *  Lazy-fetches its spans only when the user clicks to expand, so a
 *  long conversation doesn't pay the cost of fetching every trace's
 *  spans up front. */
export default function ConversationTurn({
  trace,
  turnNumber,
}: {
  trace: Trace;
  turnNumber: number;
}) {
  const [open, setOpen] = useState(false);
  const status = deriveStatus(trace);
  // Conditional SWR — the key is null until the user expands the row,
  // so no fetch fires for collapsed turns.
  const { data: spans } = useSWR<Span[]>(
    open ? `/v1/traces/${trace.id}/spans` : null,
    fetcher,
  );

  return (
    <div className="border-b border-[var(--border)] last:border-b-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full px-3 py-3 hover:bg-[#111418] transition-colors flex items-center gap-3 text-left"
        aria-expanded={open}
      >
        <span className="text-[var(--muted)] text-xs font-mono w-16 shrink-0">
          Turn {turnNumber}
        </span>
        <span className="font-mono text-xs text-[var(--muted)] w-16 shrink-0 tabular-nums">
          {formatDuration(trace.duration_ms)}
        </span>
        <span
          className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded shrink-0 ${
            STATUS_STYLES[status] ?? STATUS_STYLES.ok
          }`}
        >
          {status}
        </span>
        <span className="font-mono truncate flex-1">
          {trace.name ?? trace.id}
        </span>
        {trace.total_cost_usd > 0 && (
          <span className="text-xs text-[var(--muted)] font-mono shrink-0">
            {formatCost(trace.total_cost_usd)}
          </span>
        )}
        <Link
          href={`/traces/${trace.id}`}
          onClick={(e) => e.stopPropagation()}
          className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] shrink-0"
          title="Open trace in its own page"
        >
          ↗
        </Link>
      </button>

      {open && (
        <div className="px-3 pb-3 pt-1 bg-[#0d1014] space-y-3">
          {trace.input && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
                User input
              </div>
              <pre className="font-mono text-xs whitespace-pre-wrap break-all border border-[var(--border)] rounded p-2">
                {prettyInput(trace.input)}
              </pre>
            </div>
          )}
          {trace.output && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
                Agent output
              </div>
              <pre className="font-mono text-xs whitespace-pre-wrap break-all border border-[var(--border)] rounded p-2">
                {prettyInput(trace.output)}
              </pre>
            </div>
          )}
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
              Span timeline
              {spans ? ` (${spans.length})` : ''}
            </div>
            {spans ? (
              spans.length === 0 ? (
                <div className="text-xs text-[var(--muted)]">No spans.</div>
              ) : (
                <SpanTimeline spans={spans} />
              )
            ) : (
              <div className="text-xs text-[var(--muted)]">Loading spans…</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function prettyInput(raw: string): string {
  try {
    const parsed = JSON.parse(raw);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return raw;
  }
}
