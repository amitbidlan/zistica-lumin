import ViolationsList from '@/components/ViolationsList';

export const metadata = {
  title: 'Violations',
};

export default function ViolationsPage() {
  return (
    <div className="max-w-7xl mx-auto">
      <h1 className="text-lg font-semibold mb-5">Policy violations</h1>
      <ViolationsList />
    </div>
  );
}
