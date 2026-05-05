import AgentList from '@/components/AgentList';

export const metadata = {
  title: 'Agents',
};

export default function AgentsPage() {
  return (
    <div className="max-w-7xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">
          Your agents
        </h1>
        <p className="text-[var(--muted)] text-sm mt-1.5 max-w-2xl">
          Every distinct trace name appears here as an agent, grouped by
          framework. Click one to see its recent runs, model mix, and any
          policy violations. Live activity refreshes every 5 seconds.
        </p>
      </div>
      <AgentList />
    </div>
  );
}
