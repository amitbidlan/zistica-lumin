'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';

import {
  fetchTemplateDetail,
  instantiateTemplate,
  TemplateDetail,
  TemplateField,
} from '@/lib/api';

/**
 * Renders a template's field schema as a form. On save, calls
 * POST /v1/firewall/templates/{id}/instantiate with the operator's
 * field values; the API compiles the condition and creates a
 * policy in shadow mode (per §10.1).
 *
 * Field types supported in v1:
 *   - multi-select  → checkboxes from `choices`
 *   - select        → radio buttons from `choices`
 *   - text          → input
 *   - number        → input type=number
 *
 * The modal is full-screen on mobile, centered card on desktop.
 * Closes on Esc / overlay click / "Cancel" / successful save.
 * On success it routes to /policies/{name} so the operator lands
 * on the new rule's edit page (where they can promote out of
 * shadow via ModeToggle).
 */
export default function TemplateInstantiateModal({
  templateId,
  onClose,
}: {
  templateId: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const { data: detail, error, isLoading } = useSWR<TemplateDetail>(
    `template:${templateId}`,
    () => fetchTemplateDetail(templateId),
  );

  const [name, setName] = useState('');
  const [fieldValues, setFieldValues] = useState<Record<string, unknown>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Pre-seed name + field defaults once detail loads
  useEffect(() => {
    if (!detail) return;
    if (!name) setName(detail.id);
    const seed: Record<string, unknown> = {};
    for (const f of detail.fields) {
      if (f.default !== undefined) seed[f.id] = f.default;
    }
    setFieldValues((prev) => ({ ...seed, ...prev }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail]);

  // Esc closes the modal
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  async function handleSave() {
    if (!detail) return;
    if (!name.trim()) {
      setSubmitError('Name is required');
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      const out = await instantiateTemplate(templateId, {
        name: name.trim(),
        field_values: fieldValues,
      });
      // Route to the new policy's edit page
      router.push(`/policies/${encodeURIComponent(out.name)}`);
      router.refresh();
      onClose();
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="card p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        {error ? (
          <div className="text-rose-400 text-sm">
            Failed to load template: {String(error.message ?? error)}
          </div>
        ) : isLoading || !detail ? (
          <div className="text-center text-[var(--muted)] py-8">Loading…</div>
        ) : (
          <>
            <div className="flex items-start gap-3">
              <div className="text-3xl">{detail.icon ?? '📋'}</div>
              <div className="flex-1 min-w-0">
                <h2 className="text-lg font-semibold">{detail.name}</h2>
                <p className="text-sm text-[var(--muted)] mt-1">
                  {detail.summary}
                </p>
              </div>
            </div>

            <div className="space-y-4 border-t border-[var(--border)] pt-4">
              <FormField
                label="Rule name"
                hint="Used as the deduplication key + shown on every decision."
              >
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="form-input font-mono"
                  placeholder={detail.id}
                  required
                />
              </FormField>

              {detail.fields.map((field) => (
                <RenderField
                  key={field.id}
                  field={field}
                  value={fieldValues[field.id]}
                  onChange={(v) =>
                    setFieldValues({ ...fieldValues, [field.id]: v })
                  }
                />
              ))}
            </div>

            <div className="border-t border-[var(--border)] pt-3 text-xs text-[var(--muted)]">
              The new rule will start in <strong>shadow mode</strong> — it
              will record decisions but never block live traffic until you
              promote it to enforce on the policy edit page.
            </div>

            {submitError ? (
              <div className="card p-3 text-sm text-rose-400 border-rose-500/40">
                {submitError}
              </div>
            ) : null}

            <div className="flex items-center justify-end gap-2 border-t border-[var(--border)] pt-3">
              <button
                onClick={onClose}
                disabled={submitting}
                className="px-3 py-1.5 rounded text-sm border border-[var(--border)] hover:bg-[var(--surface-hover)]"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={submitting}
                className="px-4 py-1.5 rounded text-sm font-medium bg-[var(--accent)] text-[var(--accent-foreground)] disabled:opacity-50"
              >
                {submitting ? 'Saving…' : 'Save in shadow mode'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}


function RenderField({
  field,
  value,
  onChange,
}: {
  field: TemplateField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (field.type === 'multi-select') {
    const selected = new Set((value as string[]) ?? []);
    return (
      <FormField label={field.label} hint={field.hint}>
        <div className="space-y-1.5">
          {field.choices?.map((c) => (
            <label
              key={c.id}
              className="flex items-center gap-2 text-sm cursor-pointer"
            >
              <input
                type="checkbox"
                checked={selected.has(c.id)}
                onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(c.id);
                  else next.delete(c.id);
                  onChange(Array.from(next));
                }}
              />
              <span>{c.label}</span>
            </label>
          ))}
        </div>
      </FormField>
    );
  }

  if (field.type === 'select') {
    return (
      <FormField label={field.label} hint={field.hint}>
        <div className="space-y-1.5">
          {field.choices?.map((c) => (
            <label
              key={c.id}
              className="flex items-start gap-2 text-sm cursor-pointer"
            >
              <input
                type="radio"
                name={field.id}
                checked={value === c.id}
                onChange={() => onChange(c.id)}
                className="mt-0.5"
              />
              <span>{c.label}</span>
            </label>
          ))}
        </div>
      </FormField>
    );
  }

  if (field.type === 'number') {
    return (
      <FormField label={field.label} hint={field.hint}>
        <input
          type="number"
          value={(value as number) ?? ''}
          onChange={(e) => onChange(Number(e.target.value))}
          className="form-input"
        />
      </FormField>
    );
  }

  return (
    <FormField label={field.label} hint={field.hint}>
      <input
        value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value)}
        className="form-input"
      />
    </FormField>
  );
}


function FormField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <div className="text-sm font-medium mb-1">{label}</div>
      {hint ? (
        <div className="text-xs text-[var(--muted)] mb-2 whitespace-pre-line">
          {hint}
        </div>
      ) : null}
      {children}
    </label>
  );
}
