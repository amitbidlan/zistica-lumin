import TemplateGallery from '@/components/TemplateGallery';

export const dynamic = 'force-dynamic';

export const metadata = {
  title: 'Templates',
};

export default function TemplatesPage() {
  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-5">
        <h1 className="text-lg font-semibold">Rule templates</h1>
        <p className="text-sm text-[var(--muted)] mt-1">
          Pre-built firewall rule shapes for common use cases. Pick a
          template, fill the form, save in shadow mode. No regex, no
          Python — operators with no Python/regex experience can author
          a working rule in under a minute.
        </p>
      </div>
      <TemplateGallery />
    </div>
  );
}
