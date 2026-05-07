'use client';

import { useState } from 'react';

import {
  FirewallMode,
  ModeChangeForecast,
  setPolicyMode,
} from '@/lib/api';

/**
 * Mode toggle with confirmation + back-test forecast (§5.4 / §8.2).
 *
 * Operators flip a policy from shadow → enforce only after seeing
 * what it would have blocked. The toggle calls the API which runs
 * the forecast synchronously (back-tests the rule against the last
 * 30 days of decisions) and returns the count + a few example
 * trace_ids; we render that in a confirmation step before applying.
 *
 * The pattern:
 *   1. User clicks a non-current mode chip
 *   2. We call /v1/policies/{name}/mode — it runs the forecast AND
 *      flips immediately (the API doesn't have a "preview" endpoint
 *      separate from "apply" yet; that's a §11 enhancement)
 *   3. We show a toast with the forecast count
 *
 * For the dashboard slice this is good enough — operators can flip
 * back to shadow any time, and the forecast is informational. A
 * future preview-then-apply step can land on top.
 */
export default function ModeToggle({
  policyName,
  currentMode,
  onChange,
}: {
  policyName: string;
  currentMode: FirewallMode | string;
  onChange?: (newMode: FirewallMode, forecast: ModeChangeForecast) => void;
}) {
  const [busy, setBusy] = useState<FirewallMode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastForecast, setLastForecast] = useState<{
    mode: FirewallMode;
    forecast: ModeChangeForecast;
  } | null>(null);

  async function flip(target: FirewallMode) {
    if (busy || target === currentMode) return;
    setBusy(target);
    setError(null);
    try {
      const out = await setPolicyMode(policyName, target);
      setLastForecast({ mode: target, forecast: out.forecast });
      onChange?.(target, out.forecast);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="inline-flex items-center rounded-md border border-[var(--border)] overflow-hidden text-sm">
        {(['shadow', 'flag', 'enforce'] as FirewallMode[]).map((m) => (
          <button
            key={m}
            onClick={() => flip(m)}
            disabled={busy !== null}
            className={
              'px-3 py-1.5 transition-colors ' +
              (m === currentMode
                ? 'bg-[var(--accent)] text-[var(--accent-foreground)] font-medium'
                : 'text-[var(--muted)] hover:bg-[var(--surface-hover)]') +
              (busy === m ? ' opacity-60' : '') +
              (busy && busy !== m ? ' opacity-40' : '')
            }
            title={modeHelp(m)}
          >
            {m}
            {busy === m && <span className="ml-1 animate-pulse">…</span>}
          </button>
        ))}
      </div>

      {error && <div className="text-xs text-rose-400">{error}</div>}

      {lastForecast && (
        <div className="text-xs space-y-1">
          <div className="text-[var(--foreground-soft)]">
            Switched to{' '}
            <span className="font-medium">{lastForecast.mode}</span>. Over the
            last 30 days this policy fired{' '}
            <span className="font-medium">
              {lastForecast.forecast.would_have_blocked}
            </span>{' '}
            time(s).
          </div>
          {lastForecast.forecast.examples.length > 0 && (
            <div className="text-[var(--muted)]">
              Recent traces:{' '}
              {lastForecast.forecast.examples.map((tid, i) => (
                <span key={tid}>
                  <a
                    href={`/traces/${tid}`}
                    className="font-mono hover:underline text-[var(--accent)]"
                  >
                    {tid.slice(0, 8)}…
                  </a>
                  {i < lastForecast.forecast.examples.length - 1 ? ', ' : ''}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}


function modeHelp(m: FirewallMode): string {
  switch (m) {
    case 'shadow':
      return 'Record decisions but never block live traffic.';
    case 'flag':
      return 'Record + return decision=flag (does not cancel the action).';
    case 'enforce':
      return 'Take the configured action (block / require approval / rewrite).';
  }
}
