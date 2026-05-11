import AgentDetail from '@/components/AgentDetail';

export const metadata = {
  title: 'Agent',
};

// AgentDetail reads ``?window`` via the URL-state hooks, which requires
// dynamic rendering. Matches /agents and /agents/framework/[key].
export const dynamic = 'force-dynamic';

interface PageProps {
  params: { name: string };
}

export default function AgentDetailPage({ params }: PageProps) {
  // The :path-style API route accepts dots in the agent name. Decode
  // here so the dashboard and the API agree on the literal name.
  const name = decodeURIComponent(params.name);
  return (
    <div className="max-w-7xl mx-auto">
      <AgentDetail name={name} />
    </div>
  );
}
