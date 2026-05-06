import AgentList from '@/components/AgentList';

export const metadata = {
  title: 'Agents',
};

// Force dynamic rendering: AgentList reads filters from URL search params
// via the URL-backed state hooks, which Next.js requires to be either
// wrapped in Suspense or rendered dynamically. These pages always need
// to fetch live data anyway (SWR + WebSocket), so prerendering an empty
// shell adds no value — making them dynamic is cleaner than scattering
// Suspense boundaries.
export const dynamic = 'force-dynamic';

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
