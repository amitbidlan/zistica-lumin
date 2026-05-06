'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import {
  ActivityLabel,
  AgentListResponse,
  AgentSummary,
  fetcher,
  formatActivity,
  formatCost,
  formatDuration,
} from '@/lib/api';
import { useTraceStream, WSMessage, ConnectionState } from '@/lib/websocket';
import { useUrlNumber, useUrlString } from '@/lib/url-state';
import Sparkline from '@/components/Sparkline';

const WINDOW_OPTIONS = [
  { hours: 1,  label: '1h'  },
  { hours: 24, label: '24h' },
  { hours: 24 * 7, label: '7d' },
];

// Cap at 2 rows of cards on the xl 3-column grid (= 6 cards). A
// long-tail Python SDK section with 30+ agents was overwhelming the
// "all frameworks" view; truncating + linking to the per-framework
// view restores scannability. The cap is dropped when the user
// selects a specific framework — that mode IS the expanded view.
const SECTION_CAP = 6;

const PROJECT_LABEL: Record<string, string> = {
  openclaw:  'OpenClaw',
  mastra:    'Mastra',
  voltagent: 'VoltAgent',
  default:   'Python SDK',
};

const PROJECT_ICON: Record<string, string> = {
  openclaw:  '🦞',
  mastra:    '⚡',
  voltagent: '🔌',
  default:   '🐍',
};

const PROJECT_DESC: Record<string, string> = {
  openclaw:  'Personal-AI assistants via @lumin-io/openclaw',
  mastra:    'TypeScript agents via @lumin-io/mastra',
  voltagent: 'OTel-native TS agents via @lumin-io/voltagent',
  default:   'Python SDK · LangChain / CrewAI / Anthropic / direct',
};

// Anything outside the four known frameworks is something the operator
// shipped with their own X-Lumin-Project header. Surface it as "Custom"
// with the raw key shown in parentheses so they can still tell which
// integration it is, without the ugly bare ◆ on the section header.
function projectLabel(p: string): string {
  if (p in PROJECT_LABEL) return PROJECT_LABEL[p];
  return p ? `Custom (${p})` : 'Custom';
}
function projectIcon(p: string): string {
  return PROJECT_ICON[p] ?? '🧪';
}
function projectDesc(p: string): string {
  if (p in PROJECT_DESC) return PROJECT_DESC[p];
  return p ? `Custom integration · X-Lumin-Project: ${p}` : 'Custom integration';
}


export default function AgentList({
  lockedFramework,
}: {
  /** When set, the project filter is fixed to this framework and
   * cannot be toggled from inside this component. The framework filter
   * pill row is replaced with a "← All frameworks" link. Used by the
   * dedicated /agents/framework/[key] route. */
  lockedFramework?: string;
} = {}) {
  // Filter state is URL-backed so refresh / deep-link / back-button
  // preserve what the operator was looking at. ``lockedFramework``
  // (route param) takes precedence over the URL ``project`` filter
  // because the dedicated /agents/framework/[key] route IS the
  // hard-narrowed view.
  const [window_, setWindow] = useUrlNumber('window', 24);
  const [search, setSearch] = useUrlString('q', '');
  const [projectFilterRaw, setProjectFilter] = useUrlString('project', 'all');
  const [providerFilter, setProviderFilter] = useUrlString('provider', 'all');
  const projectFilter = lockedFramework ?? projectFilterRaw;

  const params = new URLSearchParams();
  params.set('window_hours', String(window_));
  if (search)                    params.set('search', search);
  if (projectFilter  !== 'all')  params.set('project',  projectFilter);
  if (providerFilter !== 'all')  params.set('provider', providerFilter);

  const swrKey = `/v1/agents?${params.toString()}`;
  const { mutate } = useSWRConfig();

  // Track agents that just received a span — flip them to "active"
  // immediately so the dot pulses without waiting for the refetch
  // round-trip. Keyed by agent name; expires after a few seconds via
  // the natural cadence of refetches.
  const [liveAgents, setLiveAgents] = useState<Set<string>>(new Set());
  const liveTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const traceToAgent = useRef<Map<string, string>>(new Map());
  const refetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // WebSocket subscription — every span/trace event triggers a
  // debounced refetch + an optimistic "active" flip on the
  // affected agent's card. The 5s polling fallback below only
  // kicks in when WS is disconnected.
  const wsState = useTraceStream(
    useCallback(
      (msg: WSMessage) => {
        // Resolve agent name from the message
        let agentName: string | null = null;
        let traceId: string | null = null;
        if (msg.type === 'new_trace') {
          agentName = msg.trace.name;
          traceId = msg.trace.id;
        } else if (msg.type === 'new_span') {
          traceId = msg.trace_id;
          // Span events don't carry the agent name. We learn the
          // mapping from preceding new_trace events; if we missed
          // it (page just loaded), the next refetch fills it in.
          agentName = traceToAgent.current.get(msg.trace_id) ?? null;
        }

        if (traceId && agentName) {
          traceToAgent.current.set(traceId, agentName);
        }

        if (agentName) {
          // Flip to "active" immediately
          setLiveAgents((prev) => {
            if (prev.has(agentName!)) return prev;
            const next = new Set(prev);
            next.add(agentName!);
            return next;
          });
          // Clear after 6s if no further activity (longer than the
          // server's 30s "active" threshold but short enough to
          // visibly settle)
          const existing = liveTimers.current.get(agentName);
          if (existing) clearTimeout(existing);
          const t = setTimeout(() => {
            setLiveAgents((prev) => {
              const next = new Set(prev);
              next.delete(agentName!);
              return next;
            });
            liveTimers.current.delete(agentName!);
          }, 6000);
          liveTimers.current.set(agentName, t);
        }

        // Debounced refetch — coalesce bursts (e.g. 35-span runaway
        // batch) into one server query rather than hammering it.
        if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
        refetchTimerRef.current = setTimeout(() => {
          mutate(swrKey);
        }, 400);
      },
      [swrKey, mutate],
    ),
  );

  // Cleanup all timers on unmount
  useEffect(() => {
    const timers = liveTimers.current;
    return () => {
      timers.forEach((t) => clearTimeout(t));
      timers.clear();
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
    };
  }, []);

  const { data, error, isLoading } = useSWR<AgentListResponse>(
    swrKey,
    fetcher,
    {
      // Polling is the FALLBACK — when WS is connected we don't need
      // it. Disconnected? Resume polling so the UI keeps moving.
      refreshInterval: wsState !== 'connected' ? 5000 : 0,
      keepPreviousData: true,
    },
  );

  // When the WS reaches "connected" after a disconnect window, force a
  // one-shot refetch — the server may have processed traces during the
  // gap that we missed.
  const wasDisconnected = useRef(false);
  useEffect(() => {
    if (wsState === 'disconnected') {
      wasDisconnected.current = true;
    } else if (wsState === 'connected' && wasDisconnected.current) {
      mutate(swrKey);
      wasDisconnected.current = false;
    }
  }, [wsState, swrKey, mutate]);

  const distinctProviders = useMemo(() => {
    if (!data) return [];
    const s = new Set<string>();
    for (const a of data.agents) for (const p of a.providers) s.add(p);
    return Array.from(s).sort();
  }, [data]);

  const grouped = useMemo(() => {
    if (!data) return new Map<string, AgentSummary[]>();
    const m = new Map<string, AgentSummary[]>();
    for (const a of data.agents) {
      const list = m.get(a.project) ?? [];
      list.push(a);
      m.set(a.project, list);
    }
    return m;
  }, [data]);

  if (error) {
    return (
      <div className="card p-4 text-rose-400">
        Failed to load agents: {String(error.message ?? error)}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* ── Top filter bar ──────────────────────────── */}
      <div className="card p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <LiveIndicator state={wsState} />
          <span className="opacity-30">·</span>
          <span className="text-[11px] text-[var(--muted)] uppercase tracking-wider">
            Window
          </span>
          <div className="flex items-center gap-1">
            {WINDOW_OPTIONS.map((w) => (
              <button
                key={w.hours}
                onClick={() => setWindow(w.hours)}
                className={
                  window_ === w.hours ? 'pill pill-active' : 'pill'
                }
              >
                {w.label}
              </button>
            ))}
          </div>
          <span className="opacity-30">·</span>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search agent name…"
            className="bg-transparent border border-[var(--border)] rounded-md px-3 py-1 text-sm text-[var(--foreground)] focus:outline-none focus:border-[var(--accent)] placeholder:text-[var(--muted-soft)] flex-1 min-w-0 max-w-xs"
          />
        </div>

        {data && data.projects.length > 0 ? (
          <div className="flex items-center gap-2 flex-wrap">
            {lockedFramework ? (
              <>
                <Link
                  href="/agents"
                  className="pill"
                  title="Back to all frameworks"
                >
                  ← All frameworks
                </Link>
                <span className="opacity-30 mx-1">·</span>
                <span className="text-[11px] text-[var(--muted)] uppercase tracking-wider">
                  Viewing
                </span>
                <span className="pill pill-active">
                  <span>{projectIcon(lockedFramework)}</span>
                  <span>{projectLabel(lockedFramework)}</span>
                </span>
              </>
            ) : (
              <>
                <span className="text-[11px] text-[var(--muted)] uppercase tracking-wider">
                  Framework
                </span>
                <button
                  onClick={() => setProjectFilter('all')}
                  className={projectFilter === 'all' ? 'pill pill-active' : 'pill'}
                >
                  all
                </button>
                {data.projects.map((p) => (
                  <Link
                    key={p}
                    href={`/agents/framework/${encodeURIComponent(p)}`}
                    className="pill"
                    title={`View all ${projectLabel(p)} agents`}
                  >
                    <span>{projectIcon(p)}</span>
                    <span>{projectLabel(p)}</span>
                  </Link>
                ))}
              </>
            )}

            {distinctProviders.length > 0 ? (
              <>
                <span className="opacity-30 mx-1">·</span>
                <span className="text-[11px] text-[var(--muted)] uppercase tracking-wider">
                  Provider
                </span>
                <button
                  onClick={() => setProviderFilter('all')}
                  className={providerFilter === 'all' ? 'pill pill-active' : 'pill'}
                >
                  all
                </button>
                {distinctProviders.map((p) => (
                  <button
                    key={p}
                    onClick={() => setProviderFilter(p)}
                    className={providerFilter === p ? 'pill pill-active' : 'pill'}
                  >
                    {p}
                  </button>
                ))}
              </>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* ── Body ─────────────────────────────────────── */}
      {isLoading || !data ? (
        <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>
      ) : data.agents.length === 0 ? (
        <div className="card p-8 text-center">
          <div className="text-[var(--foreground-soft)] mb-2">
            No agents in the last {data.window_hours}h
            {search ? ` matching “${search}”` : ''}
            {projectFilter !== 'all' ? ` in ${projectLabel(projectFilter)}` : ''}.
          </div>
          {data.older_data_exists ? (
            <div className="mt-4 flex items-center justify-center gap-2 flex-wrap">
              <span className="text-[var(--muted)] text-xs">
                But there&rsquo;s older activity. Try
              </span>
              {WINDOW_OPTIONS
                .filter((w) => w.hours > window_)
                .map((w) => (
                  <button
                    key={w.hours}
                    onClick={() => setWindow(w.hours)}
                    className="pill pill-active"
                  >
                    {w.label}
                  </button>
                ))}
            </div>
          ) : (
            <div className="text-[var(--muted)] text-xs mt-2">
              Send a span to <code className="font-mono">POST /v1/spans</code> to see your first agent.
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-8">
          {Array.from(grouped.entries())
            .sort(([a]: [string, AgentSummary[]], [b]: [string, AgentSummary[]]) =>
              projectLabel(a).localeCompare(projectLabel(b)))
            .map(([project, agents]: [string, AgentSummary[]]) => {
              // When viewing all frameworks, cap each section at SECTION_CAP
              // and surface "view all" → expand. When the user has already
              // filtered to a single framework, this section IS the expanded
              // view, so no cap.
              const isExpanded = projectFilter !== 'all';
              const visible = isExpanded ? agents : agents.slice(0, SECTION_CAP);
              const hidden = agents.length - visible.length;
              return (
                <section key={project}>
                  <div className="flex items-baseline gap-3 mb-3">
                    <span className="text-2xl leading-none">
                      {projectIcon(project)}
                    </span>
                    <h2 className="text-lg font-semibold tracking-tight">
                      {projectLabel(project)}
                    </h2>
                    <span className="text-[var(--muted)] text-xs">
                      {agents.length} agent{agents.length === 1 ? '' : 's'}
                    </span>
                    <span className="text-[var(--muted-soft)] text-xs hidden md:inline">
                      · {projectDesc(project)}
                    </span>
                    {hidden > 0 ? (
                      <Link
                        href={`/agents/framework/${encodeURIComponent(project)}`}
                        className="ml-auto text-[11px] uppercase tracking-wider text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
                      >
                        view all {agents.length} →
                      </Link>
                    ) : null}
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {visible.map((a) => (
                      <AgentCard
                        key={`${a.project}::${a.name}`}
                        agent={a}
                        liveOverride={liveAgents.has(a.name)}
                      />
                    ))}
                    {hidden > 0 ? (
                      <Link
                        href={`/agents/framework/${encodeURIComponent(project)}`}
                        className="card card-interactive p-5 flex flex-col items-center justify-center text-center gap-1 group"
                      >
                        <span className="text-2xl opacity-60 group-hover:opacity-100 transition-opacity">
                          {projectIcon(project)}
                        </span>
                        <span className="text-sm text-[var(--foreground-soft)] group-hover:text-[var(--foreground)] transition-colors">
                          + {hidden} more
                        </span>
                        <span className="text-[10px] uppercase tracking-wider text-[var(--muted)]">
                          view all {agents.length} →
                        </span>
                      </Link>
                    ) : null}
                  </div>
                </section>
              );
            })}
        </div>
      )}
    </div>
  );
}


function AgentCard({
  agent,
  liveOverride = false,
}: {
  agent: AgentSummary;
  liveOverride?: boolean;
}) {
  // WS-driven override: a span just landed for this agent → show as
  // "active" immediately, even if the cached server value is stale.
  const activity: ActivityLabel = liveOverride ? 'active' : agent.activity;
  const isThinking = (agent.active_traces ?? 0) > 0 || liveOverride;
  return (
    <Link
      href={`/agents/${encodeURIComponent(agent.name)}`}
      className="card card-interactive block p-5"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <ActivityDot activity={activity} />
          <span
            className="font-mono text-sm font-medium truncate"
            title={agent.name}
          >
            {agent.name}
          </span>
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {isThinking ? (
            <ThinkingBadge count={agent.active_traces} live={liveOverride} />
          ) : null}
          {agent.has_violations ? (
            <span className="badge badge-rose">
              {agent.violation_count} violation{agent.violation_count === 1 ? '' : 's'}
            </span>
          ) : !isThinking ? (
            <span className="text-[10px] text-[var(--muted)]">
              {formatActivity(agent.seconds_since_last_seen)}
            </span>
          ) : null}
        </div>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-3">
        <Metric label="Traces" value={String(agent.trace_count)} />
        <Metric label="Cost"   value={formatCost(agent.total_cost_usd)} />
        <Metric label="Avg dur" value={formatDuration(agent.avg_duration_ms)} />
      </div>

      {/* Activity sparkline — last 60 min, 5-min buckets */}
      <div className="mt-3 flex items-center justify-between gap-2">
        <span className="text-[9px] uppercase tracking-wider text-[var(--muted-soft)]">
          last 60min
        </span>
        <Sparkline buckets={agent.activity_buckets ?? []} width={120} height={18} />
      </div>

      <div className="mt-3 pt-3 border-t border-[var(--border)] flex items-center justify-between gap-2 text-[10px] text-[var(--muted)]">
        <div className="flex items-center gap-1.5 min-w-0 flex-1 flex-wrap">
          {agent.providers.length > 0 ? (
            agent.providers.map((p) => <ProviderPill key={p} name={p} />)
          ) : (
            <span>no LLM calls</span>
          )}
          {agent.top_model ? (
            <span
              className="font-mono truncate text-[10px] text-[var(--muted-soft)]"
              title={agent.top_model}
            >
              {agent.top_model}
            </span>
          ) : null}
        </div>
        <span className="flex-shrink-0">
          {agent.error_rate > 0 ? (
            <span className="text-amber-300">
              {(agent.error_rate * 100).toFixed(0)}% errors
            </span>
          ) : (
            <span className="text-emerald-400">healthy</span>
          )}
        </span>
      </div>
    </Link>
  );
}


function ThinkingBadge({
  count,
  live,
}: {
  count: number;
  live: boolean;
}) {
  // "thinking" means at least one in-flight trace right now (or a
  // span just landed via WS). Pulses to draw the eye — operators
  // glance at the grid to see "what's running right now?"
  const label =
    count > 1 ? `${count} thinking` : live ? 'live now' : 'thinking…';
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded"
      style={{
        background: 'rgba(16, 185, 129, 0.1)',
        color: '#6ee7b7',
        borderColor: 'rgba(16, 185, 129, 0.4)',
      }}
      title={
        count > 0
          ? `${count} trace${count === 1 ? '' : 's'} in flight`
          : 'span just landed via WebSocket'
      }
    >
      <span className="activity-dot active" style={{ width: 6, height: 6 }} />
      {label}
    </span>
  );
}


function ProviderPill({ name }: { name: string }) {
  const cls = (
    name.includes('anthropic') ? 'badge badge-orange' :
    name.includes('openai')    ? 'badge badge-emerald' :
    name.includes('google') || name.includes('gemini') ? 'badge badge-blue' :
    name.includes('ollama')    ? 'badge badge-violet' :
    'badge badge-slate'
  );
  return <span className={cls}>{name}</span>;
}


function ActivityDot({ activity }: { activity: ActivityLabel }) {
  return <span className={`activity-dot ${activity}`} title={activity} />;
}


function LiveIndicator({ state }: { state: ConnectionState }) {
  // Treat the WS state semantically — "connected" = green pulse,
  // anything else = "polling" (grey + label) so an operator knows
  // the dashboard's still moving even when the socket is flaky.
  if (state === 'connected') {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-emerald-400"
        title="Live updates streaming via WebSocket"
      >
        <span className="activity-dot active" style={{ width: 6, height: 6 }} />
        live
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[var(--muted)]"
      title={state === 'connecting' ? 'Connecting to live stream…' : 'WebSocket unavailable — polling fallback'}
    >
      <span className="activity-dot dormant" style={{ width: 6, height: 6 }} />
      {state === 'connecting' ? 'connecting' : 'polling'}
    </span>
  );
}


function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
        {label}
      </div>
      <div className="metric-value text-sm">{value}</div>
    </div>
  );
}
