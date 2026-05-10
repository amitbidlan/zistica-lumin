'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { API_BASE, fetcher } from '@/lib/api';

/**
 * Operator-friendly UI for the tenant-isolation firewall.
 *
 * Page layout:
 *   1. Profile picker (4 named profiles)
 *   2. Fine-grained override toggles
 *   3. Effective-settings panel (live merge preview)
 *   4. Recent activity panel (volume context — last 24h blocks/redactions)
 *   5. Save bar (sticky, with reset + logging-only confirmation)
 */

type ResetMode = 'between-users' | 'between-channels' | 'never';

interface L3Detectors {
  vault_exact?: boolean;
  structural_pattern?: boolean;
  presidio?: boolean;
}

interface FirewallProfile {
  agent_id: string;
  security_profile: string | null;
  overrides: {
    enableTenantIsolation?: boolean;
    blockShellTools?: boolean;
    blockWebTools?: boolean;
    resetMemoryBetweenUsers?: ResetMode;
    hideOtherUsersData?: boolean;
    recordSecurityEvents?: boolean | number;
    l3Detectors?: L3Detectors;
    sharedPaths?: string[];
    failClosedOnMissingWorkspace?: boolean;
  };
  updated_at?: string | null;
  updated_by?: string | null;
}

interface ResolvedSettings {
  enforce: boolean;
  blockShellTools: boolean;
  blockWebTools: boolean;
  resetMemoryBetweenUsers: ResetMode;
  hideOtherUsersData: boolean;
  l3Detectors: { vault_exact: boolean; structural_pattern: boolean; presidio: boolean };
  recordSecurityEvents: number;
  failClosedOnMissingWorkspace: boolean;
}

const PROFILES = [
  {
    id: 'strict',
    label: 'Strict',
    who: 'Healthcare, finance, legal, regulated multi-tenant SaaS',
    defaults: 'Every protection on. Fails closed. Full Presidio NER. Records every event.',
  },
  {
    id: 'standard',
    label: 'Standard',
    who: 'Default — multi-user bots (Slack/Telegram support, sales triage)',
    defaults: 'File isolation, blocks shells + web, wipes memory between users, scrubs IDs/emails, records every event.',
  },
  {
    id: 'light',
    label: 'Light',
    who: 'Single-team internal bots, dev environments',
    defaults: 'File isolation only, no shell/web blocking, vault-only redaction, samples 10% of events.',
  },
  {
    id: 'logging-only',
    label: 'Logging only',
    who: 'Lumin as Langfuse-style observer',
    defaults: 'Records traces and spans. Never blocks. Use during early rollout before turning enforcement on.',
  },
] as const;

// Mirror server-side resolveSecuritySettings just enough for an
// in-page preview of "if I save, this is what the plugin will use".
// Exact values for each profile MUST match server (kept in sync
// manually — covered by unit tests).
const PROFILE_DEFAULTS: Record<string, ResolvedSettings> = {
  strict: {
    enforce: true,
    blockShellTools: true,
    blockWebTools: true,
    resetMemoryBetweenUsers: 'between-users',
    hideOtherUsersData: true,
    l3Detectors: { vault_exact: true, structural_pattern: true, presidio: true },
    recordSecurityEvents: 1,
    failClosedOnMissingWorkspace: true,
  },
  standard: {
    enforce: true,
    blockShellTools: true,
    blockWebTools: true,
    resetMemoryBetweenUsers: 'between-users',
    hideOtherUsersData: true,
    l3Detectors: { vault_exact: true, structural_pattern: true, presidio: false },
    recordSecurityEvents: 1,
    failClosedOnMissingWorkspace: false,
  },
  light: {
    enforce: true,
    blockShellTools: false,
    blockWebTools: false,
    resetMemoryBetweenUsers: 'between-channels',
    hideOtherUsersData: false,
    l3Detectors: { vault_exact: true, structural_pattern: false, presidio: false },
    recordSecurityEvents: 0.1,
    failClosedOnMissingWorkspace: false,
  },
  'logging-only': {
    enforce: false,
    blockShellTools: false,
    blockWebTools: false,
    resetMemoryBetweenUsers: 'never',
    hideOtherUsersData: false,
    l3Detectors: { vault_exact: false, structural_pattern: false, presidio: false },
    recordSecurityEvents: 1,
    failClosedOnMissingWorkspace: false,
  },
};

function resolveEffectiveSettings(
  profileId: string,
  overrides: FirewallProfile['overrides'],
): ResolvedSettings {
  const base = PROFILE_DEFAULTS[profileId] ?? PROFILE_DEFAULTS.standard;
  const out: ResolvedSettings = { ...base };
  if (overrides.enableTenantIsolation !== undefined) out.enforce = overrides.enableTenantIsolation;
  if (overrides.blockShellTools !== undefined) out.blockShellTools = overrides.blockShellTools;
  if (overrides.blockWebTools !== undefined) out.blockWebTools = overrides.blockWebTools;
  if (overrides.resetMemoryBetweenUsers !== undefined)
    out.resetMemoryBetweenUsers = overrides.resetMemoryBetweenUsers;
  if (overrides.hideOtherUsersData !== undefined) {
    out.hideOtherUsersData = overrides.hideOtherUsersData;
    out.l3Detectors = overrides.hideOtherUsersData
      ? { vault_exact: true, structural_pattern: true, presidio: true }
      : { vault_exact: false, structural_pattern: false, presidio: false };
  }
  if (overrides.l3Detectors) {
    out.l3Detectors = { ...out.l3Detectors, ...overrides.l3Detectors };
  }
  if (overrides.recordSecurityEvents !== undefined) {
    out.recordSecurityEvents =
      typeof overrides.recordSecurityEvents === 'boolean'
        ? overrides.recordSecurityEvents ? 1 : 0
        : Math.max(0, Math.min(1, overrides.recordSecurityEvents));
  }
  if (overrides.failClosedOnMissingWorkspace !== undefined)
    out.failClosedOnMissingWorkspace = overrides.failClosedOnMissingWorkspace;
  return out;
}

export default function FirewallSettings() {
  const { data, error, isLoading, mutate } = useSWR<FirewallProfile>(
    '/v1/admin/firewall/profile?agent_id=_default',
    fetcher,
    { refreshInterval: 10000 },
  );

  if (error) {
    return (
      <div className="card p-4 text-rose-400">
        Failed to load firewall settings: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>;
  }

  return <SettingsForm initial={data} onSaved={() => mutate()} />;
}

function SettingsForm({
  initial,
  onSaved,
}: {
  initial: FirewallProfile;
  onSaved: () => void;
}) {
  const [profile, setProfile] = useState<string>(
    initial.security_profile ?? 'standard',
  );
  const [overrides, setOverrides] = useState<FirewallProfile['overrides']>(
    initial.overrides ?? {},
  );
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const dirty =
    profile !== (initial.security_profile ?? 'standard') ||
    JSON.stringify(overrides) !== JSON.stringify(initial.overrides ?? {});

  function update<K extends keyof FirewallProfile['overrides']>(
    key: K,
    val: FirewallProfile['overrides'][K] | undefined,
  ) {
    setOverrides((prev) => {
      const next = { ...prev };
      if (val === undefined) delete next[key];
      else next[key] = val;
      return next;
    });
  }

  function resetOverrides() {
    setOverrides({});
  }

  async function handleSave() {
    if (profile === 'logging-only') {
      const ok = window.confirm(
        'Switching to "Logging only" turns OFF tenant-isolation enforcement.\n\n' +
        '• Lumin will record traces and spans, but never block tool calls.\n' +
        '• Cross-session leaks become possible if multiple users share an agent.\n' +
        '• Use this only during initial rollout / debugging.\n\n' +
        'Continue?',
      );
      if (!ok) return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const resp = await fetch(
        `${API_BASE}/v1/admin/firewall/profile?agent_id=_default`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            security_profile: profile,
            overrides,
          }),
        },
      );
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try {
          const body = await resp.json();
          if (body.detail) msg = String(body.detail);
        } catch {
          /* leave fallback */
        }
        throw new Error(msg);
      }
      setSavedAt(new Date().toLocaleTimeString());
      onSaved();
    } catch (e) {
      setSaveError(String((e as Error).message ?? e));
    } finally {
      setSaving(false);
    }
  }

  const effective = useMemo(
    () => resolveEffectiveSettings(profile, overrides),
    [profile, overrides],
  );

  const overrideCount = Object.keys(overrides).length;

  return (
    <div className="space-y-6">
      {/* Profile picker */}
      <section className="card p-5">
        <h2 className="text-lg font-medium mb-1">Security profile</h2>
        <p className="text-sm text-[var(--muted)] mb-4">
          One-knob shorthand. Each profile sets sensible defaults for every
          protection below. Override individual toggles in the section
          below to deviate.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {PROFILES.map((p) => (
            <label
              key={p.id}
              className={`card p-4 cursor-pointer hover:border-[var(--accent)] ${
                profile === p.id ? 'border-[var(--accent)] ring-1 ring-[var(--accent)]/40' : ''
              }`}
            >
              <div className="flex items-start gap-3">
                <input
                  type="radio"
                  name="profile"
                  value={p.id}
                  checked={profile === p.id}
                  onChange={() => setProfile(p.id)}
                  className="mt-1"
                />
                <div>
                  <div className="font-medium">{p.label}</div>
                  <div className="text-xs text-[var(--muted)] mt-0.5">{p.who}</div>
                  <div className="text-xs text-[var(--foreground-soft)] mt-2 leading-relaxed">
                    {p.defaults}
                  </div>
                </div>
              </div>
            </label>
          ))}
        </div>
      </section>

      {/* Fine-grained overrides */}
      <section className="card p-5">
        <div className="flex items-baseline justify-between mb-1">
          <h2 className="text-lg font-medium">Fine-grained overrides</h2>
          <button
            type="button"
            onClick={resetOverrides}
            disabled={overrideCount === 0}
            className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] underline disabled:no-underline disabled:opacity-50"
            aria-label="Reset all overrides to profile defaults"
          >
            {overrideCount === 0 ? 'No overrides' : `Reset overrides (${overrideCount})`}
          </button>
        </div>
        <p className="text-sm text-[var(--muted)] mb-4">
          These override the profile&apos;s defaults. Leave any toggle on
          &ldquo;Use profile default&rdquo; to inherit.
        </p>

        <div className="space-y-4">
          <ToggleRow
            label="Enable tenant isolation"
            help="Master switch. When off, plugin records traces but never blocks."
            value={overrides.enableTenantIsolation}
            onChange={(v) => update('enableTenantIsolation', v)}
          />
          <ToggleRow
            label="Block shell tools"
            help="Block exec, shell, bash, python, ruby — anything that can run shell commands."
            value={overrides.blockShellTools}
            onChange={(v) => update('blockShellTools', v)}
          />
          <ToggleRow
            label="Block web tools"
            help="Block web_fetch, http_get, curl — anything that can leak data via URL."
            value={overrides.blockWebTools}
            onChange={(v) => update('blockWebTools', v)}
          />
          <SelectRow
            label="Reset memory between users"
            help="When the bot's conversation memory gets wiped."
            value={overrides.resetMemoryBetweenUsers}
            options={[
              { value: 'between-users', label: 'Between users (strictest)' },
              { value: 'between-channels', label: 'Between channels (per-channel history)' },
              { value: 'never', label: 'Never (only safe with architectural per-user contexts)' },
            ]}
            onChange={(v) => update('resetMemoryBetweenUsers', v as ResetMode | undefined)}
          />
          <ToggleRow
            label="Hide other users' data from AI"
            help="Scrub names, emails, customer IDs, organisations from prompts before the model sees them."
            value={overrides.hideOtherUsersData}
            onChange={(v) => update('hideOtherUsersData', v)}
          />
          <ToggleRow
            label="Record security events"
            help="Log every block/redaction to the dashboard's Violations table."
            value={
              typeof overrides.recordSecurityEvents === 'number'
                ? overrides.recordSecurityEvents > 0
                : overrides.recordSecurityEvents
            }
            onChange={(v) => update('recordSecurityEvents', v)}
          />
        </div>
      </section>

      {/* Effective settings (live merge preview) */}
      <EffectiveSettingsPanel
        profileId={profile}
        effective={effective}
        overrideCount={overrideCount}
      />

      {/* Recent activity volume */}
      <RecentActivityPanel />

      {/* Save bar */}
      <div className="sticky bottom-0 -mx-2 px-3 py-3 bg-[var(--background)] border-t border-[var(--card-border)] flex items-center justify-between">
        <div className="text-xs text-[var(--muted)]">
          {savedAt ? <span className="text-emerald-400">Saved at {savedAt}</span> : null}
          {!savedAt && initial.updated_at ? (
            <span>Last saved {new Date(initial.updated_at).toLocaleString()}</span>
          ) : null}
          {saveError ? (
            <div className="text-rose-400 mt-1" role="alert">
              {saveError}
            </div>
          ) : null}
        </div>
        <button
          type="button"
          onClick={handleSave}
          disabled={saving || !dirty}
          className="pill pill-active disabled:opacity-50"
          aria-label="Save firewall settings"
        >
          {saving ? 'Saving…' : dirty ? 'Save settings' : 'No changes'}
        </button>
      </div>
    </div>
  );
}

function EffectiveSettingsPanel({
  profileId,
  effective,
  overrideCount,
}: {
  profileId: string;
  effective: ResolvedSettings;
  overrideCount: number;
}) {
  const profileBase = PROFILE_DEFAULTS[profileId] ?? PROFILE_DEFAULTS.standard;
  const isFromOverride = (key: keyof ResolvedSettings) => {
    return JSON.stringify(effective[key]) !== JSON.stringify(profileBase[key]);
  };
  const Pill = ({ label, value, fromOverride }: { label: string; value: string; fromOverride: boolean }) => (
    <div className="flex items-center justify-between text-xs py-1.5 border-b border-[var(--card-border)] last:border-b-0">
      <span className="text-[var(--muted)]">{label}</span>
      <span className={`font-mono ${fromOverride ? 'text-amber-400' : ''}`}>
        {value}
        {fromOverride ? <span className="ml-1 opacity-60">(override)</span> : null}
      </span>
    </div>
  );

  return (
    <section className="card p-5">
      <div className="flex items-baseline justify-between mb-1">
        <h2 className="text-lg font-medium">Effective settings (preview)</h2>
        <span className="text-xs text-[var(--muted)]">
          profile: <span className="font-medium text-[var(--foreground)]">{profileId}</span>
          {overrideCount > 0 ? <> · <span className="text-amber-400">{overrideCount} override{overrideCount === 1 ? '' : 's'}</span></> : null}
        </span>
      </div>
      <p className="text-sm text-[var(--muted)] mb-3">
        Merged result of profile defaults + your overrides. Highlighted
        rows differ from the profile&apos;s defaults.
      </p>
      <div className="space-y-0">
        <Pill label="Enforce (master switch)" value={String(effective.enforce)} fromOverride={isFromOverride('enforce')} />
        <Pill label="Block shell tools" value={String(effective.blockShellTools)} fromOverride={isFromOverride('blockShellTools')} />
        <Pill label="Block web tools" value={String(effective.blockWebTools)} fromOverride={isFromOverride('blockWebTools')} />
        <Pill label="Reset memory between users" value={effective.resetMemoryBetweenUsers} fromOverride={isFromOverride('resetMemoryBetweenUsers')} />
        <Pill label="Hide other users' data" value={String(effective.hideOtherUsersData)} fromOverride={isFromOverride('hideOtherUsersData')} />
        <Pill label="L3: vault_exact" value={String(effective.l3Detectors.vault_exact)} fromOverride={JSON.stringify(effective.l3Detectors) !== JSON.stringify(profileBase.l3Detectors)} />
        <Pill label="L3: structural_pattern" value={String(effective.l3Detectors.structural_pattern)} fromOverride={JSON.stringify(effective.l3Detectors) !== JSON.stringify(profileBase.l3Detectors)} />
        <Pill label="L3: presidio (NER)" value={String(effective.l3Detectors.presidio)} fromOverride={JSON.stringify(effective.l3Detectors) !== JSON.stringify(profileBase.l3Detectors)} />
        <Pill label="Record security events (sampling)" value={String(effective.recordSecurityEvents)} fromOverride={isFromOverride('recordSecurityEvents')} />
        <Pill label="Fail closed on missing workspace" value={String(effective.failClosedOnMissingWorkspace)} fromOverride={isFromOverride('failClosedOnMissingWorkspace')} />
      </div>
    </section>
  );
}

interface ViolationsResp {
  violations: Array<{ created_at?: string | null; policy_name: string; severity?: string }>;
  total: number;
}

function RecentActivityPanel() {
  const { data, error, isLoading } = useSWR<ViolationsResp>(
    '/v1/violations?limit=200',
    fetcher,
    { refreshInterval: 30000 },
  );

  let last24h = 0;
  let blocks = 0;
  let redactions = 0;
  let sandboxBlocks = 0;
  let egressBlocks = 0;
  if (data && Array.isArray(data.violations)) {
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    for (const v of data.violations) {
      const t = v.created_at ? Date.parse(String(v.created_at) + 'Z') : NaN;
      if (!Number.isFinite(t) || t < cutoff) continue;
      last24h++;
      const name = v.policy_name || '';
      if (name.startsWith('lumin_egress_deny:')) {
        blocks++;
        egressBlocks++;
      } else if (name.startsWith('lumin_sandbox_block:')) {
        blocks++;
        sandboxBlocks++;
      } else {
        // Other policies — count as "redactions / observations" for
        // this panel's purpose. The full breakdown lives in /violations.
        redactions++;
      }
    }
  }

  return (
    <section className="card p-5">
      <h2 className="text-lg font-medium mb-1">Recent activity (last 24h)</h2>
      <p className="text-sm text-[var(--muted)] mb-4">
        Volume of blocks and redactions the firewall has produced
        recently. Use this to gauge whether your settings are too
        loose or too strict.
      </p>
      {error ? (
        <div className="text-rose-400 text-sm">Could not load activity: {String((error as Error).message ?? error)}</div>
      ) : isLoading ? (
        <div className="text-[var(--muted)] text-sm">Loading…</div>
      ) : (
        <div className="grid grid-cols-3 gap-4">
          <Stat label="Total events" value={last24h} />
          <Stat label="Sandbox blocks (L1)" value={sandboxBlocks} accent="amber" />
          <Stat label="Egress blocks (L1.5)" value={egressBlocks} accent="amber" />
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, accent }: { label: string; value: number; accent?: 'amber' | 'emerald' }) {
  const color = accent === 'amber' ? 'text-amber-400' : accent === 'emerald' ? 'text-emerald-400' : '';
  return (
    <div>
      <div className={`text-2xl font-semibold ${color}`}>{value}</div>
      <div className="text-xs text-[var(--muted)] mt-0.5">{label}</div>
    </div>
  );
}

function ToggleRow({
  label,
  help,
  value,
  onChange,
}: {
  label: string;
  help: string;
  value: boolean | undefined;
  onChange: (v: boolean | undefined) => void;
}) {
  const helpId = `help-${label.toLowerCase().replace(/[^a-z]+/g, '-')}`;
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-[var(--card-border)] last:border-b-0">
      <div className="flex-1">
        <div className="font-medium text-sm">{label}</div>
        <div id={helpId} className="text-xs text-[var(--muted)] mt-0.5">{help}</div>
      </div>
      <select
        aria-describedby={helpId}
        value={value === undefined ? '' : value ? 'on' : 'off'}
        onChange={(e) => {
          if (e.target.value === '') onChange(undefined);
          else onChange(e.target.value === 'on');
        }}
        className="bg-[var(--card)] border border-[var(--card-border)] rounded px-2 py-1 text-sm"
      >
        <option value="">Use profile default</option>
        <option value="on">On</option>
        <option value="off">Off</option>
      </select>
    </div>
  );
}

function SelectRow<T extends string>({
  label,
  help,
  value,
  options,
  onChange,
}: {
  label: string;
  help: string;
  value: T | undefined;
  options: { value: T; label: string }[];
  onChange: (v: T | undefined) => void;
}) {
  const helpId = `help-${label.toLowerCase().replace(/[^a-z]+/g, '-')}`;
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-[var(--card-border)] last:border-b-0">
      <div className="flex-1">
        <div className="font-medium text-sm">{label}</div>
        <div id={helpId} className="text-xs text-[var(--muted)] mt-0.5">{help}</div>
      </div>
      <select
        aria-describedby={helpId}
        value={value ?? ''}
        onChange={(e) => onChange((e.target.value || undefined) as T | undefined)}
        className="bg-[var(--card)] border border-[var(--card-border)] rounded px-2 py-1 text-sm max-w-[260px]"
      >
        <option value="">Use profile default</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}
