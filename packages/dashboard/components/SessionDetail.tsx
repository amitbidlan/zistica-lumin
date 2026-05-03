'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  SessionDetail as SessionDetailT,
  fetcher,
  formatCost,
  formatDuration,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import { useTraceStream, WSMessage } from '@/lib/websocket';
import ConversationTurn from './ConversationTurn';

export default function SessionDetail({ id }: { id: string }) {
  const sessionKey = `/v1/sessions/${encodeURIComponent(id)}`;
  const { mutate } = useSWRConfig();

  // Refetch the session when a new trace arrives that belongs to it.
  // We deliberately do NOT refetch on every new_span — most spans belong
  // to traces in OTHER sessions, and we don't have membership info from
  // the message alone. Inner span counts inside an open turn won't update
  // live (acceptable for v1 — the user can collapse + re-expand the turn
  // to refresh, or open the trace in its own page where spans stream live).
  const wsState = useTraceStream(
    useCallback(
      (msg: WSMessage) => {
        if (msg.type === 'new_trace' && msg.trace.session_id === id) {
          mutate(sessionKey);
        }
      },
      [id, sessionKey, mutate],
    ),
  );

  const refreshInterval = wsState !== 'connected' ? 5000 : 0;
  const { data, error } = useSWR<SessionDetailT>(sessionKey, fetcher, {
    refreshInterval,
  });

  // Catch-up on reconnect
  const wasDisconnected = useRef(false);
  useEffect(() => {
    if (wsState === 'disconnected') {
      wasDisconnected.current = true;
    } else if (wsState === 'connected' && wasDisconnected.current) {
      mutate(sessionKey);
      wasDisconnected.current = false;
    }
  }, [wsState, sessionKey, mutate]);

  if (error) {
    if ((error as { status?: number }).status === 404) {
      return (
        <div className="text-[var(--muted)]">
          Session <code className="font-mono">{id}</code> not found.
        </div>
      );
    }
    return (
      <div className="text-red-400">
        Failed to load session:{' '}
        {String((error as { message?: string }).message ?? error)}
      </div>
    );
  }
  if (!data) return <div className="text-[var(--muted)]">Loading…</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link
          href="/sessions"
          className="text-[var(--muted)] text-xs hover:text-[var(--foreground)]"
        >
          ← All sessions
        </Link>
        <span
          className="inline-flex items-center gap-1 text-xs text-[var(--muted)]"
          title={
            wsState === 'connected'
              ? 'WebSocket connected — new turns appear live'
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
        <h1 className="font-mono text-base mb-1 break-all">
          {data.session_id}
        </h1>
        <div className="text-[var(--muted)] text-xs mb-3">
          Multi-turn conversation
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
          <Metric label="Turns" value={String(data.trace_count)} />
          <Metric
            label="Wall duration"
            value={formatDuration(data.wall_duration_ms)}
          />
          <Metric label="Total cost" value={formatCost(data.total_cost_usd)} />
          <Metric label="Avg quality" value={formatScore(data.quality_score)} />
          <Metric label="Started" value={formatStartedAt(data.first_seen)} />
        </div>
      </header>

      <section>
        <h2 className="text-xs uppercase tracking-wider text-[var(--muted)] mb-2">
          Conversation timeline ({data.traces.length}{' '}
          {data.traces.length === 1 ? 'turn' : 'turns'})
        </h2>
        {data.traces.length === 0 ? (
          <div className="text-[var(--muted)] text-sm">
            No turns yet for this session.
          </div>
        ) : (
          <div className="border border-[var(--border)] rounded">
            {data.traces.map((trace, i) => (
              <ConversationTurn
                key={trace.id}
                trace={trace}
                turnNumber={i + 1}
              />
            ))}
          </div>
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
