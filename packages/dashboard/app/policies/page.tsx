import PolicyList from '@/components/PolicyList';

export const metadata = {
  title: 'Policies',
};

export default function PoliciesPage() {
  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Policies</h1>
        <p className="text-[var(--muted)] text-sm mt-1.5 max-w-2xl">
          Rules that catch agent misbehavior — cost runaways, prompt
          injection attempts, PII leaks, runaway loops. Each rule fires
          a violation when its condition matches; alerts can also POST
          to a webhook.
        </p>
      </div>
      <PolicyList />
    </div>
  );
}
