'use client';

import Link from 'next/link';
import useSWR from 'swr';
import { fetcher, Policy, PolicyListResponse } from '@/lib/api';

/**
 * Top-level list of policies — one card per policy with severity badge,
 * trigger / action pills, and condition preview. Clicking opens the
 * editor. The "+" button at the top routes to /policies/new.
 *
 * The banner at the top reflects where the engine is reading from
 * (DB once Phase 4 bootstrap has run; YAML on a fresh install before
 * the bootstrap, or when LUMIN_POLICY_FILE points at a file that has
 * been edited but not yet imported).
 */
export default function PolicyList() {
  const { data, error, isLoading, mutate } = useSWR<PolicyListResponse>(
    '/v1/policies',
    fetcher,
    { refreshInterval: 10000 },
  );

  if (error) {
    return (
      <div className="card p-4 text-rose-400">
        Failed to load policies: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <SourceBanner source={data.source} engineLoaded={data.engine_loaded} />

      <div className="flex items-center justify-between">
        <div className="text-sm text-[var(--muted)]">
          {data.policies.length} active polic{data.policies.length === 1 ? 'y' : 'ies'}
        </div>
        <Link href="/policies/new" className="pill pill-active">
          + New policy
        </Link>
      </div>

      {data.policies.length === 0 ? (
        <div className="card p-8 text-center">
          <div className="text-[var(--foreground-soft)] mb-2">
            No policies yet.
          </div>
          <div className="text-[var(--muted)] text-sm mb-4">
            Define rules that fire when an agent misbehaves —
            cost runaways, prompt injection attempts, PII leaks.
          </div>
          <Link href="/policies/new" className="pill pill-active">
            Create your first policy
          </Link>
        </div>
      ) : (
        <div className="space-y-2">
          {data.policies.map((p) => (
            <PolicyCard key={p.name} policy={p} />
          ))}
        </div>
      )}
    </div>
  );
}

function SourceBanner({
  source,
  engineLoaded,
}: {
  source: 'yaml' | 'db' | 'none';
  engineLoaded: boolean;
}) {
  if (!engineLoaded) {
    return (
      <div
        className="card p-3 text-sm"
        style={{
          background: 'rgba(244, 63, 94, 0.06)',
          borderColor: 'rgba(244, 63, 94, 0.3)',
        }}
      >
        <strong className="text-rose-400">Policy engine is disabled.</strong>{' '}
        <span className="text-[var(--muted)]">
          Set <code className="font-mono">LUMIN_POLICY_FILE</code> to bootstrap from
          YAML, or create a policy below to populate the database.
        </span>
      </div>
    );
  }
  if (source === 'yaml') {
    return (
      <div className="card p-3 text-sm">
        <strong>Loaded from YAML.</strong>{' '}
        <span className="text-[var(--muted)]">
          The first policy you create here will be saved to the database, and
          the engine will switch to DB-backed mode automatically.
        </span>
      </div>
    );
  }
  return null;
}

function PolicyCard({ policy }: { policy: Policy }) {
  const sevCls = (
    policy.severity === 'critical' ? 'badge badge-rose' :
    policy.severity === 'high'     ? 'badge badge-rose' :
    policy.severity === 'medium'   ? 'badge badge-amber' :
                                     'badge badge-slate'
  );
  const triggerCls = policy.trigger === 'span_end' ? 'badge badge-blue' : 'badge badge-violet';
  const actionCls  = (
    policy.action === 'block'             ? 'badge badge-rose' :
    policy.action === 'require_approval'  ? 'badge badge-amber' :
    policy.action === 'rewrite'           ? 'badge badge-cyan' :
    policy.action === 'allow'             ? 'badge badge-emerald' :
    policy.action === 'alert'             ? 'badge badge-orange' :
                                            'badge badge-slate'
  );
  // Slice 2 Tier 1.3: surface firewall fields when present.
  // Mode chip dims for shadow (rule records but never fires) so a
  // glance shows which rules are *actually* enforcing today.
  const modeCls = (
    policy.mode === 'enforce'  ? 'badge badge-rose' :
    policy.mode === 'flag'     ? 'badge badge-amber' :
    policy.mode === 'shadow'   ? 'badge badge-slate opacity-70' :
                                 'badge badge-slate'
  );
  const isFirewallLifecycle =
    policy.lifecycle && policy.lifecycle !== 'post_ingest';

  return (
    <Link
      href={`/policies/${encodeURIComponent(policy.name)}`}
      className="card card-interactive p-4 block"
    >
      <div className="flex items-center gap-3 mb-2 flex-wrap">
        <span className={sevCls}>{policy.severity}</span>
        <h3 className="font-mono text-sm font-medium tracking-tight">{policy.name}</h3>
        {isFirewallLifecycle ? (
          <span className="badge badge-violet">{policy.lifecycle}</span>
        ) : (
          <span className={triggerCls}>{policy.trigger}</span>
        )}
        <span className={actionCls}>{policy.action}</span>
        {policy.mode ? (
          <span className={modeCls} title={modeHelp(policy.mode)}>
            {policy.mode}
          </span>
        ) : null}
        {policy.scope_agents.length > 0 ? (
          <span className="badge badge-fuchsia">
            scoped to {policy.scope_agents.length} agent{policy.scope_agents.length === 1 ? '' : 's'}
          </span>
        ) : (
          <span className="text-[10px] text-[var(--muted)]">all agents</span>
        )}
        {policy.source === 'yaml' ? (
          <span className="text-[10px] text-[var(--muted)] ml-auto">
            from YAML
          </span>
        ) : null}
      </div>
      {policy.description ? (
        <p className="text-sm text-[var(--foreground-soft)] mb-2">
          {policy.description}
        </p>
      ) : null}
      <code className="block text-xs font-mono text-[var(--muted)] truncate">
        {policy.condition}
      </code>
    </Link>
  );
}


function modeHelp(mode: string): string {
  switch (mode) {
    case 'shadow':
      return 'Shadow — records decisions but never blocks live traffic.';
    case 'flag':
      return 'Flag — records and returns decision=flag (does not cancel the action).';
    case 'enforce':
      return 'Enforce — takes the configured action (block / require_approval / rewrite).';
    default:
      return mode;
  }
}
