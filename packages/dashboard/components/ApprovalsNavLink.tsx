'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import useSWR from 'swr';
import { ApprovalsListResponse, fetchApprovals } from '@/lib/api';

/**
 * Top-nav link for /approvals with a live pending-count badge. Polls
 * every 5s — fast enough that operators see new approvals appear in
 * the header without needing to be on the inbox page, slow enough not
 * to hammer the API. Mirrors ViolationsNavLink's pattern.
 *
 * Failures are silent — the link still renders without the badge if
 * the API is unreachable. Per Rule 7 the dashboard never crashes
 * because of policy data.
 */
export default function ApprovalsNavLink() {
  const { data } = useSWR<ApprovalsListResponse>(
    'approvals:nav-count',
    () => fetchApprovals({ state: 'pending', limit: 1 }),
    {
      refreshInterval: 5_000,
      shouldRetryOnError: false,
      revalidateOnFocus: false,
    },
  );

  const pending = data?.total ?? 0;
  const pathname = usePathname();
  const isActive =
    pathname === '/approvals' || pathname?.startsWith('/approvals/');

  return (
    <Link
      href="/approvals"
      className={
        'px-2 py-1 rounded transition-colors inline-flex items-center gap-1.5 ' +
        (isActive
          ? 'text-[var(--foreground)] bg-[var(--background-hover)]'
          : 'text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background-hover)]')
      }
    >
      Approvals
      {pending > 0 ? (
        <span
          className="inline-flex items-center justify-center min-w-[1.25rem]
                     px-1.5 rounded-full text-[10px] font-semibold
                     bg-amber-500/20 text-amber-300 border border-amber-500/40"
          title={`${pending} pending approval${pending === 1 ? '' : 's'}`}
        >
          {pending > 99 ? '99+' : pending}
        </span>
      ) : null}
    </Link>
  );
}
