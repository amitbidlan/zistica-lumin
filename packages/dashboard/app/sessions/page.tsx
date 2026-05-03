import SessionList from '@/components/SessionList';

export const metadata = {
  title: 'Sessions',
};

export default function SessionsPage() {
  return (
    <div className="max-w-7xl mx-auto">
      <h1 className="text-lg font-semibold mb-5">Sessions</h1>
      <SessionList />
    </div>
  );
}
