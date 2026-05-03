import SessionDetail from '@/components/SessionDetail';

type Params = { id: string };

export const metadata = {
  title: 'Session',
};

export default function SessionDetailPage({ params }: { params: Params }) {
  // Next.js URL-decodes path params automatically
  return (
    <div className="max-w-7xl mx-auto">
      <SessionDetail id={params.id} />
    </div>
  );
}
