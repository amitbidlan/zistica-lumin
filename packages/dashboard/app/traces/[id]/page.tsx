import TraceDetail from '@/components/TraceDetail';

type Params = { id: string };

export const metadata = {
  title: 'Trace',
};

export default function TraceDetailPage({ params }: { params: Params }) {
  return (
    <div className="max-w-7xl mx-auto">
      <TraceDetail id={params.id} />
    </div>
  );
}
