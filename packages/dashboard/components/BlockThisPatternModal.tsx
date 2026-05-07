'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';

import {
  createSuggestion,
  dismissSuggestion,
  promoteSuggestion,
  SuggestionResponse,
} from '@/lib/api';

/**
 * "Block this pattern" modal — Slice 3 PR D.
 *
 * Operator clicks the button on a fired decision row in the
 * EnforcementTimeline; this component:
 *
 *   1. POSTs /v1/policies/suggest with the decision_id → server
 *      synthesizes a draft Policy + 30-day forecast.
 *   2. Renders the draft (name, lifecycle, action, condition) +
 *      forecast count + a few example trace_ids the operator can
 *      drill into.
 *   3. On Save → POST /promote → router.push to the policy edit
 *      page where ModeToggle lets operator promote shadow → enforce
 *      after watching the timeline.
 *   4. On Dismiss → POST /dismiss; the suggestion sticks around in
 *      pattern_suggestions with dismissed_at set so we can train
 *      the suggester later on operator preferences.
 */
export default function BlockThisPatternModal({
  decisionId,
  onClose,
}: {
  decisionId: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const { data: suggestion, error, isLoading } = useSWR<SuggestionResponse>(
    `suggest:${decisionId}`,
    () => createSuggestion(decisionId),
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );

  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Pre-seed name from the draft once it loads. Operator can rename
  // before saving — auto-generated names are uniquely-suffixed but
  // not descriptive.
  useEffect(() => {
    if (suggestion && !name) {
      setName(suggestion.draft.name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggestion]);

  // Esc closes
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  async function handleSave() {
    if (!suggestion) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const out = await promoteSuggestion(suggestion.id, name.trim());
      router.push(`/policies/${encodeURIComponent(out.name)}`);
      router.refresh();
      onClose();
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDismiss() {
    if (!suggestion) {
      onClose();
      return;
    }
    try {
      await dismissSuggestion(suggestion.id);
    } catch {
      // best-effort — even if dismiss-server-side fails, we close
      // the modal locally so the operator can move on.
    }
    onClose();
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
        <div>
          <h2 className="text-lg font-semibold">Block this pattern</h2>
          <p className="text-sm text-[var(--muted)] mt-1">
            Auto-drafted from this decision. Review the rule, rename it
            if you want, then save in shadow mode. You can promote to
            enforce later from the policy edit page after watching the
            timeline.
          </p>
        </div>

        {error ? (
          <div className="card p-3 text-sm text-rose-400 border-rose-500/40">
            Failed to draft suggestion: {String(error.message ?? error)}
          </div>
        ) : isLoading || !suggestion ? (
          <div className="text-center text-[var(--muted)] py-8">
            Drafting suggestion…
          </div>
        ) : (
          <>
            <div className="space-y-2 border-t border-[var(--border)] pt-4">
              <label className="block">
                <div className="text-sm font-medium mb-1">Rule name</div>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="form-input font-mono"
                  required
                />
              </label>
            </div>

            <div className="space-y-3 border-t border-[var(--border)] pt-4 text-sm">
              <Row k="Lifecycle" v={suggestion.draft.lifecycle} />
              <Row k="Action" v={suggestion.draft.action} mono />
              <Row k="Mode" v={suggestion.draft.mode} mono />
              <Row k="Severity" v={suggestion.draft.severity} mono />
              <Row k="Priority" v={String(suggestion.draft.priority)} mono />
            </div>

            <div className="border-t border-[var(--border)] pt-3">
              <div className="text-xs uppercase tracking-wider text-[var(--muted)] mb-1">
                Generated condition
              </div>
              <pre className="text-xs bg-[var(--surface-elev)] p-3 rounded border border-[var(--border)] overflow-x-auto whitespace-pre-wrap">
                {suggestion.draft.condition}
              </pre>
            </div>

            <div className="border-t border-[var(--border)] pt-3 text-xs space-y-1">
              <div className="text-[var(--muted)] uppercase tracking-wider">
                30-day forecast
              </div>
              <div className="text-[var(--foreground-soft)]">
                Original rule fired{' '}
                <strong>{suggestion.forecast.count}</strong> time
                {suggestion.forecast.count === 1 ? '' : 's'} in the last
                30 days.
              </div>
              {suggestion.forecast.examples.length > 0 ? (
                <div className="text-[var(--muted)] flex flex-wrap gap-1">
                  Recent traces:{' '}
                  {suggestion.forecast.examples.slice(0, 5).map((tid) => (
                    <a
                      key={tid}
                      href={`/traces/${tid}`}
                      className="font-mono text-[var(--accent)] hover:underline"
                      target="_blank"
                      rel="noreferrer"
                    >
                      {tid.slice(0, 8)}…
                    </a>
                  ))}
                </div>
              ) : null}
            </div>

            {submitError ? (
              <div className="card p-3 text-sm text-rose-400 border-rose-500/40">
                {submitError}
              </div>
            ) : null}

            <div className="flex items-center justify-end gap-2 border-t border-[var(--border)] pt-3">
              <button
                onClick={handleDismiss}
                disabled={submitting}
                className="px-3 py-1.5 rounded text-sm border border-[var(--border)] hover:bg-[var(--surface-hover)] disabled:opacity-50"
              >
                Dismiss
              </button>
              <button
                onClick={handleSave}
                disabled={submitting || !name.trim()}
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


function Row({
  k,
  v,
  mono = false,
}: {
  k: string;
  v: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-3">
      <div className="w-24 shrink-0 text-[var(--muted)]">{k}</div>
      <div className={mono ? 'font-mono text-xs' : ''}>{v}</div>
    </div>
  );
}
