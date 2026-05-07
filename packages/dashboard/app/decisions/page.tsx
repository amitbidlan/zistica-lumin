import EnforcementTimeline from '@/components/EnforcementTimeline';

export const dynamic = 'force-dynamic';

export const metadata = {
  title: 'Decisions',
};

export default function DecisionsPage() {
  return (
    <div className="max-w-7xl mx-auto">
      <div className="mb-5">
        <h1 className="text-lg font-semibold">Firewall decisions</h1>
        <p className="text-sm text-[var(--muted)] mt-1">
          Every policy evaluation, in real time. Use the filters to drill in by
          decision verb, lifecycle, or agent. Shadow-mode firings show what the
          firewall <em>would</em> have done — useful for tuning a rule before
          you flip it to enforce.
        </p>
      </div>
      <EnforcementTimeline />
    </div>
  );
}
