'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';
import {
  AgentDetail as AgentDetailType,
  fetcher,
  formatActivity,
  formatCost,
  formatDuration,
  formatStartedAt,
  Policy,
  PolicyListResponse,
} from '@/lib/api';
import Sparkline from '@/components/Sparkline';

const WINDOW_OPTIONS = [
  { hours: 1,  label: '1h'  },
  { hours: 24, label: '24h' },
  { hours: 24 * 7, label: '7d' },
];

export default function AgentDetail({ name }: { name: string }) {
  const [window_, setWindow] = useState(24);

  const params = new URLSearchParams();
  params.set('window_hours', String(window_));

  const swrKey = `/v1/agents/${encodeURIComponent(name)}?${params.toString()}`;

  const { data, error, isLoading } = useSWR<AgentDetailType>(
    swrKey,
    fetcher,
    { refreshInterval: 5000 },
  );

  // Phase 3: which policies apply to this agent — un-scoped + scope.agents
  // includes name. Server already filters via `?agent=`. Polled less
  // aggressively than metrics; policies don't change every second.
  const { data: policiesResp } = useSWR<PolicyListResponse>(
    `/v1/policies?agent=${encodeURIComponent(name)}`,
    fetcher,
    { refreshInterval: 30000 },
  );

  if (error) {
    return (
      <div className="card p-4 text-rose-400">
        Failed to load agent: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link
          href="/agents"
          className="text-[var(--muted)] text-xs hover:text-[var(--foreground)] transition-colors"
        >
          ← All agents
        </Link>
        <div className="flex items-center gap-1">
          {WINDOW_OPTIONS.map((w) => (
            <button
              key={w.hours}
              onClick={() => setWindow(w.hours)}
              className={window_ === w.hours ? 'pill pill-active' : 'pill'}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      <header className="card p-6">
        <div className="flex items-center gap-3 flex-wrap">
          <ActivityDot activity={data.activity} />
          <span className="text-2xl leading-none">{projectIcon(data.project)}</span>
          <h1 className="font-mono text-lg font-medium tracking-tight">
            {data.name}
          </h1>
          <span className="text-xs text-[var(--muted)]">
            in <span className="text-[var(--foreground-soft)]">{projectLabel(data.project)}</span>
          </span>
          <div className="ml-auto flex items-center gap-1.5">
            {data.active_traces > 0 ? (
              <span
                className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded"
                style={{
                  background: 'rgba(16, 185, 129, 0.1)',
                  color: '#6ee7b7',
                  borderColor: 'rgba(16, 185, 129, 0.4)',
                }}
                title={`${data.active_traces} trace${data.active_traces === 1 ? '' : 's'} in flight`}
              >
                <span className="activity-dot active" style={{ width: 6, height: 6 }} />
                {data.active_traces} thinking
              </span>
            ) : null}
            {data.has_violations ? (
              <span className="badge badge-rose">
                {data.violation_count} violation{data.violation_count === 1 ? '' : 's'}
              </span>
            ) : (
              <span className="badge badge-emerald">healthy</span>
            )}
          </div>
        </div>
        <div className="mt-2 text-[var(--muted)] text-xs flex items-center gap-2 flex-wrap">
          last seen {formatActivity(data.seconds_since_last_seen)}
          {data.providers.length > 0 ? (
            <>
              <span>·</span>
              <div className="flex items-center gap-1.5 flex-wrap">
                {data.providers.map((p) => (
                  <ProviderPill key={p} name={p} />
                ))}
              </div>
            </>
          ) : null}
          {data.top_model ? (
            <>
              <span>·</span>
              <span className="font-mono text-[var(--foreground-soft)]">
                {data.top_model}
              </span>
            </>
          ) : null}
        </div>

        <div className="mt-6 grid grid-cols-2 md:grid-cols-5 gap-6">
          <BigMetric label="Traces"     value={String(data.trace_count)} />
          <BigMetric label="Total cost" value={formatCost(data.total_cost_usd)} />
          <BigMetric label="Tokens"     value={data.total_tokens.toLocaleString()} />
          <BigMetric label="Avg duration" value={formatDuration(data.avg_duration_ms)} />
          <BigMetric
            label="Error rate"
            value={`${(data.error_rate * 100).toFixed(0)}%`}
            tone={data.error_rate > 0.1 ? 'amber' : data.error_rate > 0 ? 'amber-soft' : 'emerald'}
          />
        </div>
      </header>

      {/* Activity over the last 60 min — a wider version of the
          card's sparkline, so detail-page operators can see the
          recent shape at a glance before drilling into the trace
          list below. */}
      <section className="card p-5">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xs uppercase tracking-wider text-[var(--muted)]">
            Activity (last 60 min · 5-min buckets)
          </h2>
          <span className="text-[10px] text-[var(--muted)]">
            {(data.activity_buckets ?? []).reduce((s, n) => s + n, 0)} traces
          </span>
        </div>
        <Sparkline
          buckets={data.activity_buckets ?? []}
          width={720}
          height={48}
        />
      </section>

      {data.violation_count > 0 ? (
        <section>
          <h2 className="text-xs uppercase tracking-wider text-[var(--muted)] mb-3">
            Violation breakdown
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <BreakdownTable
              title="By policy"
              rows={Object.entries(data.violations_by_policy).sort(
                (a, b) => b[1] - a[1],
              )}
            />
            <BreakdownTable
              title="By severity"
              rows={['critical', 'high', 'medium', 'low'].flatMap((s) =>
                data.violations_by_severity[s]
                  ? [[s, data.violations_by_severity[s]] as [string, number]]
                  : [],
              )}
            />
          </div>
        </section>
      ) : null}

      {policiesResp && policiesResp.engine_loaded ? (
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs uppercase tracking-wider text-[var(--muted)]">
              Active policies ({policiesResp.policies.length})
            </h2>
            <Link
              href="/policies"
              className="text-[10px] uppercase tracking-wider text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
            >
              manage →
            </Link>
          </div>
          {policiesResp.policies.length === 0 ? (
            <div className="card p-6 text-center text-[var(--muted)] text-sm">
              No policies apply to this agent.
            </div>
          ) : (
            <div className="space-y-2">
              {policiesResp.policies.map((p) => (
                <PolicyRow key={p.name} policy={p} agentName={name} />
              ))}
            </div>
          )}
        </section>
      ) : null}

      <section>
        <h2 className="text-xs uppercase tracking-wider text-[var(--muted)] mb-3">
          Recent traces ({data.recent_traces.length})
        </h2>
        {data.recent_traces.length === 0 ? (
          <div className="card p-6 text-center text-[var(--muted)] text-sm">
            No traces in the last {window_}h.
          </div>
        ) : (
          <div className="card overflow-hidden">
            <div className="grid grid-cols-[1fr_140px_100px_100px_80px_90px] gap-4 px-4 py-2.5 text-[10px] uppercase tracking-wider text-[var(--muted)] border-b border-[var(--border)]">
              <div>Trace</div>
              <div>Started</div>
              <div>Duration</div>
              <div>Cost</div>
              <div>Tokens</div>
              <div>Violations</div>
            </div>
            {data.recent_traces.map((t) => (
              <Link
                key={t.id}
                // ?from=agent:<name> tells TraceDetail's back link to
                // return here instead of jumping to /traces, so the
                // user's drill-down doesn't lose its place.
                href={`/traces/${t.id}?from=agent:${encodeURIComponent(name)}`}
                className="grid grid-cols-[1fr_140px_100px_100px_80px_90px] gap-4 px-4 py-2.5 text-sm border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--background-hover)] transition-colors"
              >
                <div className="font-mono text-xs truncate">{t.id}</div>
                <div className="text-[var(--muted)] text-xs">
                  {formatStartedAt(t.started_at)}
                </div>
                <div className="metric-value">{formatDuration(t.duration_ms)}</div>
                <div className="metric-value">{formatCost(t.total_cost_usd)}</div>
                <div className="metric-value text-xs">
                  {(t.total_tokens ?? 0).toLocaleString()}
                </div>
                <div>
                  {(t.violation_count ?? 0) > 0 ? (
                    <span className="badge badge-rose">
                      {t.violation_count}
                    </span>
                  ) : (
                    <span className="text-[var(--muted-soft)] text-xs">—</span>
                  )}
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}


// ---------- helpers --------------------------------------------------------


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
function projectLabel(p: string): string { return PROJECT_LABEL[p] ?? p; }
function projectIcon(p: string):  string { return PROJECT_ICON[p]  ?? '◆'; }


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


function ActivityDot({ activity }: { activity: 'active' | 'idle' | 'dormant' }) {
  return <span className={`activity-dot ${activity}`} title={activity} />;
}


function BigMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'emerald' | 'amber' | 'amber-soft' | 'rose';
}) {
  const valueCls = (
    tone === 'emerald' ? 'text-emerald-400' :
    tone === 'amber'   ? 'text-amber-400' :
    tone === 'amber-soft' ? 'text-amber-300' :
    tone === 'rose'    ? 'text-rose-400' :
    ''
  );
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1.5">
        {label}
      </div>
      <div className={`metric-value text-xl ${valueCls}`}>{value}</div>
    </div>
  );
}


function PolicyRow({ policy, agentName }: { policy: Policy; agentName: string }) {
  const sevCls = (
    policy.severity === 'critical' ? 'badge badge-rose' :
    policy.severity === 'high'     ? 'badge badge-rose' :
    policy.severity === 'medium'   ? 'badge badge-amber' :
                                     'badge badge-slate'
  );
  const scoped = policy.scope_agents.length > 0;
  const scopeLabel = scoped
    ? `scoped to ${policy.scope_agents.length} agent${policy.scope_agents.length === 1 ? '' : 's'}`
    : 'all agents';
  return (
    <Link
      href={`/policies/${encodeURIComponent(policy.name)}`}
      className="card card-interactive p-3 flex items-center gap-3 block"
    >
      <span className={sevCls}>{policy.severity}</span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <div className="font-mono text-sm truncate">{policy.name}</div>
          <span className="text-[10px] uppercase tracking-wider text-[var(--muted)]">
            {policy.trigger}
          </span>
          <span className="text-[10px] uppercase tracking-wider text-[var(--muted)]">
            · {policy.action}
          </span>
        </div>
        <div className="text-xs text-[var(--muted)] truncate font-mono mt-0.5">
          {policy.condition}
        </div>
      </div>
      <span
        className="text-[10px] text-[var(--muted)]"
        title={scoped ? policy.scope_agents.join(', ') : 'No scope.agents — applies to every agent'}
      >
        {scoped && policy.scope_agents.includes(agentName)
          ? `scoped: this agent`
          : scopeLabel}
      </span>
    </Link>
  );
}


function BreakdownTable({
  title,
  rows,
}: {
  title: string;
  rows: [string, number][];
}) {
  return (
    <div className="card overflow-hidden">
      <div className="px-4 py-2.5 text-[10px] uppercase tracking-wider text-[var(--muted)] border-b border-[var(--border)]">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="px-4 py-3 text-sm text-[var(--muted)]">No data.</div>
      ) : (
        rows.map(([k, n]) => (
          <div
            key={k}
            className="grid grid-cols-[1fr_60px] gap-4 px-4 py-2 text-sm border-b border-[var(--border)] last:border-b-0"
          >
            <div className="font-mono">{k}</div>
            <div className="text-right metric-value">{n}</div>
          </div>
        ))
      )}
    </div>
  );
}
