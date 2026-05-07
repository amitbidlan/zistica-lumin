'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';

import {
  DecisionRow,
  DecisionsListResponse,
  FirewallDecisionVerb,
  FirewallLifecycle,
  fetchDecisions,
} from '@/lib/api';
import { useUrlString } from '@/lib/url-state';

import BlockThisPatternModal from './BlockThisPatternModal';

/**
 * Live timeline of every decision the firewall produced — the
 * dashboard's primary surface for "is the firewall doing anything?"
 * and "what would have blocked yesterday?" (§8.2 of
 * AGENT_FIREWALL_SPEC.md, task #45).
 *
 * Filters: decision verb, lifecycle, agent. URL-backed via
 * useUrlString so a refresh / link share preserves the view state.
 *
 * Polls every 5s — decisions are bounded by retention (default 90d
 * per §10) so the table size stays reasonable; a heavier deployment
 * can drop refresh interval or paginate further by clicking "Load
 * more" (offset pagination). Real-time websocket fanout for new
 * decisions can land in a later slice.
 */
export default function EnforcementTimeline() {
  const [decision, setDecision] = useUrlString('decision', '');
  const [lifecycle, setLifecycle] = useUrlString('lifecycle', '');
  const [agent, setAgent] = useUrlString('agent', '');
  const [blockPatternFor, setBlockPatternFor] = useState<string | null>(null);

  const params: Parameters<typeof fetchDecisions>[0] = {
    limit: 100,
    decision: (decision || undefined) as FirewallDecisionVerb | undefined,
    lifecycle: (lifecycle || undefined) as FirewallLifecycle | undefined,
    agent: agent || undefined,
  };

  const key = ['decisions', decision, lifecycle, agent].join('|');
  const { data, error, isLoading } = useSWR<DecisionsListResponse>(
    key,
    () => fetchDecisions(params),
    { refreshInterval: 5_000 },
  );

  return (
    <div className="space-y-4">
      <div className="card p-4 flex flex-wrap items-center gap-3">
        <FilterPill
          label="Decision"
          value={decision}
          options={[
            { v: '', label: 'all' },
            { v: 'block', label: 'block' },
            { v: 'flag', label: 'flag' },
            { v: 'require_approval', label: 'approval' },
            { v: 'rewrite', label: 'rewrite' },
            { v: 'allow', label: 'allow (rare)' },
          ]}
          onChange={setDecision}
        />
        <FilterPill
          label="Lifecycle"
          value={lifecycle}
          options={[
            { v: '', label: 'all' },
            { v: 'before_proxy_call', label: 'before proxy' },
            { v: 'after_proxy_call', label: 'after proxy' },
            { v: 'before_tool_call', label: 'before tool' },
            { v: 'after_tool_call', label: 'after tool' },
            { v: 'post_ingest', label: 'post ingest' },
          ]}
          onChange={setLifecycle}
        />
        <input
          type="text"
          placeholder="Agent…"
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          className="px-3 py-1.5 rounded border bg-transparent text-sm w-40
                     border-[var(--border)] placeholder:text-[var(--muted)]"
        />
        <div className="ml-auto text-xs text-[var(--muted)]">
          {data ? `${data.decisions.length} of ${data.total}` : '—'}
        </div>
      </div>

      {error ? (
        <div className="card p-4 text-rose-400">
          Failed to load decisions: {String(error.message ?? error)}
        </div>
      ) : isLoading || !data ? (
        <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>
      ) : data.decisions.length === 0 ? (
        <div className="card p-8 text-center text-[var(--muted)]">
          No decisions match these filters.
        </div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-[var(--muted)] border-b border-[var(--border)]">
                <th className="px-3 py-2">When</th>
                <th className="px-3 py-2">Decision</th>
                <th className="px-3 py-2">Mode</th>
                <th className="px-3 py-2">Lifecycle</th>
                <th className="px-3 py-2">Policy</th>
                <th className="px-3 py-2">Agent / Tool</th>
                <th className="px-3 py-2">Trace</th>
                <th className="px-3 py-2 text-right">Latency</th>
                <th className="px-3 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {data.decisions.map((d) => (
                <DecisionRowView
                  key={d.id}
                  d={d}
                  onBlockPattern={() => setBlockPatternFor(d.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {blockPatternFor ? (
        <BlockThisPatternModal
          decisionId={blockPatternFor}
          onClose={() => setBlockPatternFor(null)}
        />
      ) : null}
    </div>
  );
}


function DecisionRowView({
  d,
  onBlockPattern,
}: {
  d: DecisionRow;
  onBlockPattern: () => void;
}) {
  return (
    <tr className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-hover)]">
      <td className="px-3 py-2 whitespace-nowrap text-[var(--muted)]">
        <Link href={`/decisions/${d.id}`} className="hover:underline">
          {formatRelative(d.decision_at)}
        </Link>
      </td>
      <td className="px-3 py-2">
        <DecisionBadge decision={d.decision} mode={d.mode_at_decision} />
      </td>
      <td className="px-3 py-2 text-[var(--muted)]">{d.mode_at_decision}</td>
      <td className="px-3 py-2 text-[var(--muted)]">{d.lifecycle}</td>
      <td className="px-3 py-2">
        <Link
          href={`/policies/${encodeURIComponent(d.policy_name)}`}
          className="hover:underline font-medium"
        >
          {d.policy_name}
        </Link>
      </td>
      <td className="px-3 py-2 text-[var(--muted)]">
        {d.agent || '—'}
        {d.tool_name ? <span className="ml-1 text-xs">/{d.tool_name}</span> : null}
      </td>
      <td className="px-3 py-2 font-mono text-xs">
        {d.trace_id ? (
          <Link href={`/traces/${d.trace_id}`} className="hover:underline">
            {d.trace_id.slice(0, 8)}…
          </Link>
        ) : (
          '—'
        )}
      </td>
      <td className="px-3 py-2 text-right text-[var(--muted)]">{d.duration_ms}ms</td>
      <td className="px-3 py-2 text-right">
        <button
          onClick={onBlockPattern}
          className="text-xs text-[var(--accent)] hover:underline"
          title="Auto-draft a new rule from this pattern"
        >
          + Block pattern
        </button>
      </td>
    </tr>
  );
}


export function DecisionBadge({
  decision,
  mode,
}: {
  decision: FirewallDecisionVerb;
  mode: string;
}) {
  // Color map: block = red, require_approval = amber, flag = yellow,
  // rewrite = blue, allow = green. Shadow mode dims everything to a
  // muted variant so an operator scanning the timeline can pick out
  // the rules that actually fired vs. those that only would have.
  const isShadow = mode === 'shadow';
  const styles = (() => {
    switch (decision) {
      case 'block':
        return isShadow
          ? 'bg-rose-500/10 text-rose-300 border-rose-500/30'
          : 'bg-rose-500/20 text-rose-200 border-rose-500/50';
      case 'require_approval':
        return isShadow
          ? 'bg-amber-500/10 text-amber-300 border-amber-500/30'
          : 'bg-amber-500/20 text-amber-200 border-amber-500/50';
      case 'flag':
        return 'bg-yellow-500/10 text-yellow-300 border-yellow-500/30';
      case 'rewrite':
        return isShadow
          ? 'bg-sky-500/10 text-sky-300 border-sky-500/30'
          : 'bg-sky-500/20 text-sky-200 border-sky-500/50';
      case 'allow':
      default:
        return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
    }
  })();
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-medium ${styles}`}
    >
      {decision}
      {isShadow && <span className="opacity-70">(shadow)</span>}
    </span>
  );
}


function FilterPill({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<{ v: string; label: string }>;
  onChange: (v: string) => void;
}) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-[var(--muted)]">
      <span>{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="px-2 py-1 rounded border bg-transparent text-sm
                   border-[var(--border)] [&>option]:bg-[var(--surface)]"
      >
        {options.map((o) => (
          <option key={o.v} value={o.v}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}


function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  // Ensure UTC interpretation when the timestamp lacks an explicit
  // offset (DuckDB returns naive timestamps, so the API's ISO string
  // is naive too).
  const d = new Date(iso.includes('Z') || iso.includes('+') ? iso : `${iso}Z`);
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 0) return d.toLocaleTimeString();
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return d.toLocaleDateString();
}
