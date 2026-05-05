import Link from 'next/link';
import PolicyEditor from '@/components/PolicyEditor';

export const metadata = {
  title: 'New policy',
};

export default function NewPolicyPage() {
  return (
    <div className="max-w-4xl mx-auto">
      <Link
        href="/policies"
        className="text-[var(--muted)] text-xs hover:text-[var(--foreground)] transition-colors"
      >
        ← All policies
      </Link>
      <div className="mt-3 mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">New policy</h1>
        <p className="text-[var(--muted)] text-sm mt-1.5 max-w-2xl">
          Define a rule that fires when an agent does something it shouldn&rsquo;t.
          The condition is a small expression — see the hint under the
          condition field for the available variables.
        </p>
      </div>
      <PolicyEditor mode="create" />
    </div>
  );
}
