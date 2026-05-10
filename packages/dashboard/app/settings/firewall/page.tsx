import FirewallSettings from '@/components/FirewallSettings';

export const metadata = {
  title: 'Firewall settings',
};

export default function FirewallSettingsPage() {
  return (
    <div className="max-w-4xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Firewall settings</h1>
        <p className="text-[var(--muted)] text-sm mt-1.5 max-w-2xl">
          Tenant-isolation defenses for your AI bot. Pick a profile that
          matches your deployment risk; fine-tune individual toggles
          when needed. Changes apply within ~30 seconds (the
          lumin-diagnostics plugin polls this page&apos;s API on every
          register + cache refresh).
        </p>
      </div>
      <FirewallSettings />
    </div>
  );
}
