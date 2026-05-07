'use client';

import { useState } from 'react';
import useSWR from 'swr';

import {
  fetchTemplates,
  TemplateSummary,
} from '@/lib/api';

import TemplateInstantiateModal from './TemplateInstantiateModal';

/**
 * Rule template gallery — Slice 3 Tier 1.05 dashboard.
 *
 * Operators pick a template card, the modal renders the template's
 * field schema as a form, on save the API compiles the condition +
 * creates a policy in shadow mode. No Python / regex required.
 *
 * Backend: /v1/firewall/templates (list) + /{id} (detail) + /{id}/instantiate.
 * Slice 2 Tier 1.05 shipped the server side; this component is the
 * UI layer that closes the authoring-DX loop.
 */
export default function TemplateGallery() {
  const { data, error, isLoading } = useSWR<{ templates: TemplateSummary[] }>(
    'templates',
    fetchTemplates,
  );
  const [openTemplate, setOpenTemplate] = useState<string | null>(null);

  if (error) {
    return (
      <div className="card p-4 text-rose-400">
        Failed to load templates: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="card p-8 text-center text-[var(--muted)]">Loading templates…</div>;
  }

  // Group by category for cleaner browsing
  const byCategory = new Map<string, TemplateSummary[]>();
  for (const t of data.templates) {
    const cat = t.category ?? 'other';
    if (!byCategory.has(cat)) byCategory.set(cat, []);
    byCategory.get(cat)!.push(t);
  }

  return (
    <div className="space-y-6">
      <div className="text-sm text-[var(--muted)]">
        {data.templates.length} template{data.templates.length === 1 ? '' : 's'} available.
        Templates ship pre-built rule shapes — pick one, fill the form,
        save in shadow mode. No regex required.
      </div>

      {Array.from(byCategory.entries()).map(([cat, tpls]) => (
        <section key={cat} className="space-y-3">
          <h2 className="text-xs uppercase tracking-wider text-[var(--muted)]">
            {cat}
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {tpls.map((t) => (
              <TemplateCard
                key={t.id}
                template={t}
                onUse={() => setOpenTemplate(t.id)}
              />
            ))}
          </div>
        </section>
      ))}

      {openTemplate ? (
        <TemplateInstantiateModal
          templateId={openTemplate}
          onClose={() => setOpenTemplate(null)}
        />
      ) : null}
    </div>
  );
}


function TemplateCard({
  template,
  onUse,
}: {
  template: TemplateSummary;
  onUse: () => void;
}) {
  return (
    <button
      onClick={onUse}
      className="card card-interactive p-4 text-left flex items-start gap-3 hover:border-[var(--accent)] transition-colors"
    >
      <div className="text-2xl">{template.icon ?? '📋'}</div>
      <div className="flex-1 min-w-0">
        <h3 className="font-medium text-sm mb-1">{template.name}</h3>
        <p className="text-xs text-[var(--muted)]">{template.summary}</p>
        <p className="text-[10px] text-[var(--muted)] mt-2 font-mono uppercase tracking-wide">
          {template.field_count} field{template.field_count === 1 ? '' : 's'}
        </p>
      </div>
    </button>
  );
}
