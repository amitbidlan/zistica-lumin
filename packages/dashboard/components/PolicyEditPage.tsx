'use client';

import Link from 'next/link';
import useSWR from 'swr';
import {
  fetcher,
  Policy,
  PolicyAuditEntry,
  formatStartedAt,
} from '@/lib/api';
import ModeToggle from '@/components/ModeToggle';
import PolicyEditor from '@/components/PolicyEditor';

/**
 * Edit-mode wrapper. Loads the policy by name, renders PolicyEditor
 * in edit mode, and shows the audit log below the form. Audit reads
 * are independent so a stale audit doesn't block the editor.
 */
export default function PolicyEditPage({ name }: { name: string }) {
  const { data: policy, error, isLoading } = useSWR<Policy>(
    `/v1/policies/${encodeURIComponent(name)}`,
    fetcher,
  );

  const { data: audit } = useSWR<{ entries: PolicyAuditEntry[]; total: number }>(
    `/v1/policies/${encodeURIComponent(name)}/audit?limit=20`,
    fetcher,
    { refreshInterval: 15000 },
  );

  if (error) {
    return (
      <div className="max-w-4xl mx-auto">
        <Link
          href="/policies"
          className="text-[var(--muted)] text-xs hover:text-[var(--foreground)]"
        >
          ← All policies
        </Link>
        <div className="card p-4 mt-4 text-rose-400">
          Failed to load policy: {String(error.message ?? error)}
        </div>
      </div>
    );
  }
  if (isLoading || !policy) {
    return (
      <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto">
      <Link
        href="/policies"
        className="text-[var(--muted)] text-xs hover:text-[var(--foreground)] transition-colors"
      >
        ← All policies
      </Link>

      <div className="mt-3 mb-8">
        <div className="flex items-center gap-3 flex-wrap">
          <h1 className="font-mono text-2xl font-medium tracking-tight">
            {policy.name}
          </h1>
          {policy.source === 'yaml' ? (
            <span className="badge badge-slate">from YAML</span>
          ) : (
            <span className="badge badge-emerald">v{policy.version}</span>
          )}
          {!policy.enabled ? (
            <span className="badge badge-rose">disabled</span>
          ) : null}
        </div>
        {policy.source === 'yaml' ? (
          <p className="text-[var(--muted)] text-sm mt-2">
            This policy was loaded from your YAML file. Saving here will create
            a DB-backed copy that overrides the YAML version on the next reload.
          </p>
        ) : null}
      </div>

      {/* Firewall mode toggle (§5.4 / §10.1). Only render when the
          API surfaced a mode value — avoids showing it for legacy
          installs whose backend hasn't run the firewall migration
          yet. */}
      {policy.mode ? (
        <section className="mb-8 card p-4 space-y-3">
          <div>
            <h2 className="text-sm font-semibold">Enforcement mode</h2>
            <p className="text-xs text-[var(--muted)] mt-1">
              Shadow records decisions without blocking. Flag returns
              decision=&apos;flag&apos; (does not cancel the action). Enforce
              applies the configured action.
            </p>
          </div>
          <ModeToggle policyName={policy.name} currentMode={policy.mode} />
        </section>
      ) : null}

      <PolicyEditor mode="edit" initial={policy} />

      {audit && audit.entries.length > 0 ? (
        <section className="mt-10">
          <h2 className="text-xs uppercase tracking-wider text-[var(--muted)] mb-3">
            Audit log ({audit.total})
          </h2>
          <div className="card overflow-hidden">
            {audit.entries.map((e) => (
              <div
                key={e.id}
                className="grid grid-cols-[120px_80px_1fr_120px] gap-4 px-4 py-2.5 text-sm border-b border-[var(--border)] last:border-b-0"
              >
                <div className="text-[var(--muted)] text-xs">
                  {formatStartedAt(e.created_at)}
                </div>
                <div>
                  <span
                    className={
                      e.action === 'create' ? 'badge badge-emerald' :
                      e.action === 'delete' ? 'badge badge-rose' :
                                              'badge badge-blue'
                    }
                  >
                    {e.action}
                  </span>
                </div>
                <div className="text-[var(--muted)] text-xs font-mono truncate">
                  <AuditDiff before={e.before} after={e.after} />
                </div>
                <div className="text-[var(--muted)] text-xs font-mono truncate">
                  {e.actor}
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}


/**
 * Render a one-line summary of what changed between two audit snapshots.
 * Skips internal columns (created_at, updated_at, version) that always
 * change on every write. Truncates long values so a row stays one line.
 */
function AuditDiff({
  before,
  after,
}: {
  before: unknown;
  after: unknown;
}) {
  const SKIP = new Set(['created_at', 'updated_at', 'version']);
  const b = (before ?? {}) as Record<string, unknown>;
  const a = (after  ?? {}) as Record<string, unknown>;

  if (!before && after) {
    return <span>created</span>;
  }
  if (before && after) {
    const changed: string[] = [];
    for (const k of Object.keys(a)) {
      if (SKIP.has(k)) continue;
      const bv = JSON.stringify(b[k]);
      const av = JSON.stringify(a[k]);
      if (bv !== av) {
        changed.push(`${k}: ${truncate(bv)} → ${truncate(av)}`);
      }
    }
    if (changed.length === 0) return <span>(no field changes)</span>;
    return <span>{changed.join(', ')}</span>;
  }
  return <span>—</span>;
}


function truncate(s: string, n = 40): string {
  if (!s) return '';
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + '…';
}
