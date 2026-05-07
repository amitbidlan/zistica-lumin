'use client';

import useSWR from 'swr';

import { fetchPanicState, setPanicDisabled, PanicState } from '@/lib/api';

/**
 * Top-of-app banner that warns when the firewall kill-switch is
 * engaged (§10.2). Only rendered when ``disabled === true`` so a
 * normal operator sees nothing. The "Re-enable" button POSTs to
 * /v1/firewall/panic_disable with disabled=false and revalidates
 * the SWR cache.
 *
 * Polls every 10s — when an oncall engineer flips the switch, every
 * other dashboard tab learns about it within one polling interval
 * without needing a websocket. Decisions returned during a panic
 * window already include reason="panic_disabled" so the timeline
 * also surfaces the state at decision granularity.
 */
export default function PanicBanner() {
  const { data, mutate } = useSWR<PanicState>(
    '/panic',
    fetchPanicState,
    { refreshInterval: 10_000 },
  );

  if (!data?.disabled) return null;

  async function reenable() {
    try {
      await setPanicDisabled(false, undefined, 'dashboard');
      await mutate();
    } catch {
      // The next poll cycle will surface any persistent failure;
      // silent here so we don't shout at the operator twice.
    }
  }

  return (
    <div className="bg-rose-500/10 border-b border-rose-500/40 text-rose-200 text-sm px-6 py-2 flex items-center justify-between gap-4">
      <div>
        <span className="font-semibold">Firewall disabled.</span>{' '}
        Every <code className="text-xs">/v1/policy/decide</code> call is
        returning <code className="text-xs">allow</code> while this kill-switch
        is engaged. Decisions are not being recorded.
      </div>
      <button
        onClick={reenable}
        className="px-3 py-1 rounded bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/50 text-xs font-medium"
      >
        Re-enable firewall
      </button>
    </div>
  );
}
