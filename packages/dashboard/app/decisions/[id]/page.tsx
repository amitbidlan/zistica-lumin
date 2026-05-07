import Link from 'next/link';

import DecisionDetailPanel from '@/components/DecisionDetailPanel';

export const dynamic = 'force-dynamic';

export const metadata = {
  title: 'Decision',
};

export default function DecisionDetailPage({
  params,
}: {
  params: { id: string };
}) {
  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-5">
        <Link
          href="/decisions"
          className="text-sm text-[var(--muted)] hover:underline"
        >
          ← All decisions
        </Link>
      </div>
      <DecisionDetailPanel decisionId={params.id} />
    </div>
  );
}
