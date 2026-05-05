import PolicyEditPage from '@/components/PolicyEditPage';

export const metadata = {
  title: 'Edit policy',
};

export default function PolicyDetailPage({
  params,
}: {
  params: { name: string };
}) {
  // Decode here so the editor sees the actual policy name, not the
  // URL-encoded form. Names with spaces or "/" are rare but supported.
  const name = decodeURIComponent(params.name);
  return <PolicyEditPage name={name} />;
}
