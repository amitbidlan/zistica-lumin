import TraceList from '@/components/TraceList';

export const metadata = {
  title: 'Traces',
};

// See app/agents/page.tsx — TraceList reads filters from URL params,
// which forces this page out of static prerendering.
export const dynamic = 'force-dynamic';

export default function TracesPage() {
  return (
    <div className="max-w-7xl mx-auto">
      <h1 className="text-lg font-semibold mb-5">Traces</h1>
      <TraceList />
    </div>
  );
}
