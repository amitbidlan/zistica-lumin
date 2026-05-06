'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  PolicyViolation,
  PolicyViolationsResponse,
  Span,
  Trace,
  fetcher,
  formatCost,
  formatDuration,
  formatScore,
  formatStartedAt,
} from '@/lib/api';
import { useTraceStream, WSMessage } from '@/lib/websocket';
import { isChatShapedTrace } from '@/lib/chat-shape';
import ChatView from './ChatView';
import SpanTimeline from './SpanTimeline';

export default function TraceDetail({ id }: { id: string }) {
  const traceKey = `/v1/traces/${id}`;
  const spansKey = `/v1/traces/${id}/spans`;
  const violationsKey = `/v1/violations?trace_id=${id}`;
  const { mutate } = useSWRConfig();
  const [tab, setTab] = useState<'spans' | 'chat' | 'policy'>('spans');
  // Once we've classified this trace as chat-shaped we want the
  // first render after data loads to land on the Chat tab — but we
  // shouldn't keep flipping the tab back if the user manually
  // switches to Spans. ``initialModeApplied`` is the latch.
  const [initialModeApplied, setInitialModeApplied] = useState(false);

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
  // Violations only fetched when the user opens the Policy tab — avoids
  // an extra API call on every trace page when the engine isn't in use.
  const violationsQ = useSWR<PolicyViolationsResponse>(
    tab === 'policy' ? violationsKey : null,
    fetcher,
    { refreshInterval: tab === 'policy' ? refreshInterval : 0 },
  );

  // Catch-up revalidation: when we reach 'connected' and the session
  // has *ever* gone through 'disconnected', force a one-shot refetch
  // so any spans broadcast during the gap surface in the cache.
  //
  // We track "ever was disconnected" via a ref rather than checking
  // immediate previous state — React may render the transient
  // 'connecting' state between 'disconnected' and 'connected' (more
  // visible on slower runners like CI), which would otherwise mask
  // the disconnect→connect transition. First connect (mount → connected,
  // never disconnected) skips the mutate — SWR's initial fetch already
  // covered that path.
  const wasDisconnected = useRef(false);
  useEffect(() => {
    if (wsState === 'disconnected') {
      wasDisconnected.current = true;
    } else if (wsState === 'connected' && wasDisconnected.current) {
      mutate(traceKey);
      mutate(spansKey);
      wasDisconnected.current = false;
    }
  }, [wsState, traceKey, spansKey, mutate]);

  // Auto-default to the Chat tab when the trace is chat-shaped, but
  // only on first classification — once the operator picks a tab
  // manually, the latch keeps their choice. Runs after data loads;
  // the `initialModeApplied` guard prevents flipping back if the
  // SWR cache revalidates with a different shape.
  useEffect(() => {
    if (initialModeApplied) return;
    if (!traceQ.data || !spansQ.data) return;
    if (isChatShapedTrace(traceQ.data, spansQ.data)) {
      setTab('chat');
    }
    setInitialModeApplied(true);
  }, [initialModeApplied, traceQ.data, spansQ.data]);

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
  const chatShaped = isChatShapedTrace(trace, spans);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <BackLink />
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
        <ThinkingBreakdown spans={spans} />
      </header>

      <section>
        <div className="flex items-center gap-1 border-b border-[var(--border)] mb-3">
          {/* The Chat tab is conditional: surfacing it on every trace
              (e.g. a Python SDK function-call agent) would be noisy.
              It only appears for chat-shaped traces — chat-shaped
              integrations also default into it. */}
          {chatShaped ? (
            <TabButton active={tab === 'chat'} onClick={() => setTab('chat')}>
              Chat
            </TabButton>
          ) : null}
          <TabButton active={tab === 'spans'} onClick={() => setTab('spans')}>
            Spans ({spans.length})
          </TabButton>
          <TabButton active={tab === 'policy'} onClick={() => setTab('policy')}>
            Policy
            {trace.has_violations ? (
              <span className="ml-1.5 inline-block h-1.5 w-1.5 rounded-full bg-red-500 align-middle" />
            ) : null}
          </TabButton>
        </div>

        {tab === 'chat' ? (
          <ChatView trace={trace} spans={spans} />
        ) : tab === 'spans' ? (
          spans.length === 0 ? (
            <div className="text-[var(--muted)] text-sm">
              No spans for this trace.
            </div>
          ) : (
            <SpanTimeline spans={spans} />
          )
        ) : (
          <PolicyTab trace={trace} violationsQ={violationsQ} />
        )}
      </section>
    </div>
  );
}

/**
 * Context-aware back link. Reads `?from=...` to figure out where the
 * user came from. Clicking a trace from /agents/<name> appends
 * `?from=agent:<encoded-name>` so the back link returns there;
 * `?from=session:<id>` works the same way; otherwise default to
 * /traces.
 *
 * Without this, every back-click after drilling-into-a-trace from
 * the agent grid jumped to /traces and lost the operator's place.
 */
function BackLink() {
  const params = useSearchParams();
  const from = params.get('from') ?? '';
  let href = '/traces';
  let label = '← All traces';
  if (from.startsWith('agent:')) {
    const name = from.slice('agent:'.length);
    href = `/agents/${name}`;
    label = `← ${decodeURIComponent(name)}`;
  } else if (from.startsWith('session:')) {
    const sid = from.slice('session:'.length);
    href = `/sessions/${sid}`;
    label = `← Session ${decodeURIComponent(sid)}`;
  }
  return (
    <Link
      href={href}
      className="text-[var(--muted)] text-xs hover:text-[var(--foreground)] transition-colors"
    >
      {label}
    </Link>
  );
}


function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  const cls = active
    ? 'text-[var(--foreground)] border-[var(--accent)]'
    : 'text-[var(--muted)] border-transparent hover:text-[var(--foreground)]';
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 text-xs uppercase tracking-wider border-b-2 transition-colors ${cls}`}
    >
      {children}
    </button>
  );
}

function PolicyTab({
  trace,
  violationsQ,
}: {
  trace: Trace;
  violationsQ: ReturnType<typeof useSWR<PolicyViolationsResponse>>;
}) {
  if (violationsQ.error) {
    return (
      <div className="text-red-400 text-sm">
        Failed to load violations: {String(violationsQ.error.message ?? violationsQ.error)}
      </div>
    );
  }
  if (!violationsQ.data) {
    return <div className="text-[var(--muted)] text-sm">Loading…</div>;
  }
  const violations = violationsQ.data.violations;
  if (violations.length === 0) {
    return (
      <div className="border border-[var(--border)] rounded p-4 flex items-center gap-2 text-sm">
        <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
        <span>No policy violations on this trace.</span>
      </div>
    );
  }

  return (
    <div className="border border-[var(--border)] rounded">
      <div className="grid grid-cols-[1.5fr_90px_1fr_90px_120px] gap-4 px-3 py-2 text-xs uppercase tracking-wider text-[var(--muted)] border-b border-[var(--border)]">
        <div>Policy</div>
        <div>Severity</div>
        <div>Condition</div>
        <div>Action</div>
        <div>Time</div>
      </div>
      {violations.map((v) => (
        <ViolationRow key={v.id} v={v} />
      ))}
    </div>
  );
}

function ViolationRow({ v }: { v: PolicyViolation }) {
  return (
    <div className="grid grid-cols-[1.5fr_90px_1fr_90px_120px] gap-4 px-3 py-2 text-sm border-b border-[var(--border)] last:border-b-0">
      <div className="font-mono">{v.policy_name}</div>
      <div>
        <SeverityBadge sev={v.severity} />
      </div>
      <div className="font-mono text-xs text-[var(--muted)] truncate" title={v.condition_text ?? ''}>
        {v.condition_text}
        {v.actual_value !== null ? (
          <span className="ml-2 text-[var(--foreground)]">→ {v.actual_value}</span>
        ) : null}
      </div>
      <div className="text-xs uppercase tracking-wider">{v.action_taken}</div>
      <div className="text-[var(--muted)] text-xs">
        {v.created_at ? new Date(v.created_at).toLocaleTimeString() : '—'}
      </div>
    </div>
  );
}

function SeverityBadge({ sev }: { sev: string }) {
  const styles: Record<string, string> = {
    low: 'bg-slate-700/40 text-slate-300 border-slate-600',
    medium: 'bg-amber-900/30 text-amber-300 border-amber-700',
    high: 'bg-red-900/40 text-red-300 border-red-700',
    critical: 'bg-fuchsia-900/40 text-fuchsia-200 border-fuchsia-700',
  };
  return (
    <span
      className={`inline-block text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded ${
        styles[sev] ?? styles.low
      }`}
    >
      {sev}
    </span>
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

function ThinkingBreakdown({ spans }: { spans: Span[] }) {
  let thinkingTokens = 0;
  let responseTokens = 0;
  let thinkingCost = 0;
  let responseCost = 0;
  for (const s of spans) {
    if (s.span_subtype === 'thinking') {
      thinkingTokens += s.thinking_tokens ?? 0;
      thinkingCost += s.cost_usd ?? 0;
    } else if (s.span_subtype === 'response') {
      responseTokens += s.tokens_output ?? 0;
      responseCost += s.cost_usd ?? 0;
    }
  }
  if (thinkingTokens === 0 && responseTokens === 0) return null;

  return (
    <div
      className="mt-4 pt-3 border-t border-[var(--border)] grid grid-cols-2 md:grid-cols-4 gap-4 text-sm"
      data-testid="thinking-breakdown"
    >
      <Metric
        label="Thinking tokens"
        value={thinkingTokens > 0 ? `~${thinkingTokens.toLocaleString()}` : '—'}
      />
      <Metric
        label="Thinking cost"
        value={thinkingCost > 0 ? formatCost(thinkingCost) : '—'}
      />
      <Metric
        label="Response tokens"
        value={responseTokens > 0 ? responseTokens.toLocaleString() : '—'}
      />
      <Metric
        label="Response cost"
        value={responseCost > 0 ? formatCost(responseCost) : '—'}
      />
    </div>
  );
}
