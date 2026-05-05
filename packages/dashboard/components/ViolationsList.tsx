'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';
import {
  PolicySeverity,
  PolicyViolation,
  PolicyViolationsResponse,
  PolicyViolationStats,
  fetcher,
} from '@/lib/api';

const SEVERITIES: PolicySeverity[] = ['critical', 'high', 'medium', 'low'];

export default function ViolationsList() {
  const [severity, setSeverity] = useState<PolicySeverity | 'all'>('all');
  const [policyName, setPolicyName] = useState<string>('');

  const params = new URLSearchParams();
  params.set('limit', '100');
  if (severity !== 'all') params.set('severity', severity);
  if (policyName) params.set('policy_name', policyName);

  const swrKey = `/v1/violations?${params.toString()}`;

  const { data, error, isLoading } = useSWR<PolicyViolationsResponse>(
    swrKey,
    fetcher,
    { refreshInterval: 5000 },
  );

  const stats = useSWR<PolicyViolationStats>(
    '/v1/violations/stats',
    fetcher,
    { refreshInterval: 5000 },
  );

  if (error) {
    return (
      <div className="card p-4 text-rose-400">
        Failed to load violations: {String(error.message ?? error)}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats summary — one card per severity, accent-colored value */}
      {stats.data && stats.data.total > 0 ? (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {SEVERITIES.map((s) => {
            const n = stats.data!.by_severity[s] ?? 0;
            return (
              <div key={s} className="card p-4">
                <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1.5">
                  {s}
                </div>
                <div className={`metric-value text-2xl ${severityValueClass(s)}`}>
                  {n}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {/* Filter row */}
      <div className="card p-4 flex items-center gap-3 flex-wrap text-xs">
        <span className="text-[11px] text-[var(--muted)] uppercase tracking-wider">
          Severity
        </span>
        <button
          onClick={() => setSeverity('all')}
          className={severity === 'all' ? 'pill pill-active' : 'pill'}
        >
          all
        </button>
        {SEVERITIES.map((s) => (
          <button
            key={s}
            onClick={() => setSeverity(s)}
            className={severity === s ? 'pill pill-active' : 'pill'}
          >
            {s}
          </button>
        ))}
        <span className="opacity-30 mx-1">·</span>
        <span className="text-[11px] text-[var(--muted)] uppercase tracking-wider">
          Policy
        </span>
        <input
          value={policyName}
          onChange={(e) => setPolicyName(e.target.value)}
          placeholder="filter by name…"
          className="form-input max-w-xs"
          style={{ padding: '0.25rem 0.625rem', fontSize: 12 }}
        />
      </div>

      {/* Body */}
      {isLoading || !data ? (
        <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>
      ) : data.violations.length === 0 ? (
        <div className="card p-8 text-center">
          <span className="inline-flex items-center gap-2 text-[var(--foreground-soft)] text-sm">
            <span className="activity-dot active" style={{ width: 8, height: 8 }} />
            No violations
            {severity !== 'all' || policyName
              ? ' matching the current filters.'
              : ' yet.'}
          </span>
          {severity === 'all' && !policyName ? (
            <div className="text-[var(--muted)] text-xs mt-2">
              Set <code className="font-mono">policy_file</code> in
              {' '}<code className="font-mono">lumin.configure(…)</code> to start
              enforcing rules.
            </div>
          ) : null}
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="grid grid-cols-[1.4fr_90px_1.6fr_90px_140px_140px] gap-4 px-4 py-2.5 text-[10px] uppercase tracking-wider text-[var(--muted)] border-b border-[var(--border)]">
            <div>Policy</div>
            <div>Severity</div>
            <div>Condition</div>
            <div>Action</div>
            <div>Trace</div>
            <div>Time</div>
          </div>
          {data.violations.map((v) => (
            <Row key={v.id} v={v} />
          ))}
        </div>
      )}
    </div>
  );
}


function Row({ v }: { v: PolicyViolation }) {
  return (
    <Link
      href={`/traces/${v.trace_id}`}
      className="grid grid-cols-[1.4fr_90px_1.6fr_90px_140px_140px] gap-4 px-4 py-2.5 text-sm border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--background-hover)] transition-colors"
    >
      <div className="font-mono truncate">{v.policy_name}</div>
      <div>
        <SeverityBadge sev={v.severity} />
      </div>
      <div
        className="font-mono text-xs text-[var(--muted)] truncate"
        title={v.condition_text ?? ''}
      >
        {v.condition_text}
        {v.actual_value !== null ? (
          <span className="ml-2 text-[var(--foreground)]">
            → {v.actual_value}
          </span>
        ) : null}
      </div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--muted)]">
        {v.action_taken}
      </div>
      <div className="font-mono text-xs truncate text-[var(--muted)]">
        {v.trace_id}
      </div>
      <div className="text-[var(--muted)] text-xs">
        {v.created_at ? new Date(v.created_at).toLocaleTimeString() : '—'}
      </div>
    </Link>
  );
}


function SeverityBadge({ sev }: { sev: string }) {
  const cls =
    sev === 'critical' ? 'badge badge-fuchsia' :
    sev === 'high'     ? 'badge badge-rose' :
    sev === 'medium'   ? 'badge badge-amber' :
                         'badge badge-slate';
  return <span className={cls}>{sev}</span>;
}


/** Metric tile value tone — keeps the severity color visible at a
 *  glance even from across the room. */
function severityValueClass(sev: PolicySeverity): string {
  if (sev === 'critical') return 'text-fuchsia-400';
  if (sev === 'high')     return 'text-rose-400';
  if (sev === 'medium')   return 'text-amber-400';
  return 'text-slate-400';
}
