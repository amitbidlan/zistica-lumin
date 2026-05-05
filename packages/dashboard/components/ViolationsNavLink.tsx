'use client';

import Link from 'next/link';
import useSWR from 'swr';
import { PolicyViolationStats, fetcher } from '@/lib/api';

/**
 * Sidebar nav link for the /violations page. Polls the stats endpoint
 * every 10s; shows a red dot when any critical/high violations exist.
 *
 * Failures are silent — if the API is unreachable the link still
 * renders, just without the indicator. Per Rule 7 the dashboard
 * never crashes because of policy data.
 */
export default function ViolationsNavLink() {
  const { data } = useSWR<PolicyViolationStats>(
    '/v1/violations/stats',
    fetcher,
    {
      refreshInterval: 10_000,
      shouldRetryOnError: false,
      revalidateOnFocus: false,
    },
  );

  const urgent =
    (data?.by_severity?.critical ?? 0) + (data?.by_severity?.high ?? 0);

  return (
    <Link
      href="/violations"
      className="text-[var(--muted)] hover:text-[var(--foreground)] transition-colors inline-flex items-center gap-1.5"
    >
      Violations
      {urgent > 0 ? (
        <span
          className="inline-block h-1.5 w-1.5 rounded-full bg-red-500"
          title={`${urgent} critical/high violation${urgent === 1 ? '' : 's'}`}
        />
      ) : null}
    </Link>
  );
}
