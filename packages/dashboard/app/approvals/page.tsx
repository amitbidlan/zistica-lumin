import ApprovalsInbox from '@/components/ApprovalsInbox';

export const dynamic = 'force-dynamic';

export const metadata = {
  title: 'Approvals',
};

export default function ApprovalsPage() {
  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-5">
        <h1 className="text-lg font-semibold">Pending approvals</h1>
        <p className="text-sm text-[var(--muted)] mt-1">
          Tool calls that fired a <code className="text-xs">require_approval</code>{' '}
          rule and are waiting for an operator decision. Click Allow or Deny
          to resolve. Denied calls are cached per session — the agent can't
          immediately retry the same action.
        </p>
      </div>
      <ApprovalsInbox />
    </div>
  );
}
