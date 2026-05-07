'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { API_BASE, Policy, PolicyAction, PolicySeverity } from '@/lib/api';

/**
 * Form for creating or editing a policy. The same component handles
 * both flows — `mode="create"` → POST /v1/policies and routes to the
 * detail page after success; `mode="edit"` → PUT to update + DELETE
 * button + scope.agents auto-populated from the existing policy.
 *
 * Scope.agents is freeform comma-separated text rather than a fancy
 * picker so it stays usable when the agent registry doesn't exist yet
 * (a brand-new install has no traces, so no agent names to pick from).
 */
export default function PolicyEditor({
  mode,
  initial,
}: {
  mode: 'create' | 'edit';
  initial?: Policy;
}) {
  const router = useRouter();

  const [name, setName] = useState(initial?.name ?? '');
  const [description, setDescription] = useState(initial?.description ?? '');
  const [trigger, setTrigger] = useState<'span_end' | 'trace_end'>(
    initial?.trigger ?? 'span_end',
  );
  const [condition, setCondition] = useState(initial?.condition ?? '');
  // The Policy.action union now includes the firewall verbs (allow,
  // block, require_approval, rewrite) alongside the legacy
  // post_ingest verbs (flag, alert). The editor doesn't yet expose
  // the firewall verbs (separate slice — operators set them via
  // YAML or the API for now), but the local state has to accept the
  // wider union so loading an existing firewall rule doesn't trip
  // the type checker.
  const [action, setAction] = useState<PolicyAction>(initial?.action ?? 'flag');
  const [severity, setSeverity] = useState<PolicySeverity>(
    initial?.severity ?? 'medium',
  );
  const [webhookUrl, setWebhookUrl] = useState(initial?.webhook_url ?? '');
  const [scopeAgents, setScopeAgents] = useState(
    (initial?.scope_agents ?? []).join(', '),
  );
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);

  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr] = useState<string | null>(null);
  const [conditionWarn, setConditionWarn] = useState<string | null>(null);

  // Cheap client-side parse hint — catches obviously broken expressions
  // before the round-trip. Server still validates definitively.
  useEffect(() => {
    setConditionWarn(localConditionLint(condition, trigger));
  }, [condition, trigger]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setSubmitErr(null);

    const scope = scopeAgents
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    try {
      if (mode === 'create') {
        const res = await fetch(`${API_BASE}/v1/policies`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: name.trim(),
            description: description || null,
            trigger,
            condition,
            action,
            severity,
            webhook_url: webhookUrl || null,
            scope_agents: scope,
            enabled,
          }),
        });
        if (!res.ok) {
          const err = await safeError(res);
          throw new Error(err);
        }
        router.push(`/policies/${encodeURIComponent(name.trim())}`);
        router.refresh();
      } else {
        const res = await fetch(
          `${API_BASE}/v1/policies/${encodeURIComponent(initial!.name)}`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              description: description || null,
              trigger,
              condition,
              action,
              severity,
              webhook_url: webhookUrl || null,
              scope_agents: scope,
              enabled,
            }),
          },
        );
        if (!res.ok) {
          const err = await safeError(res);
          throw new Error(err);
        }
        router.refresh();
      }
    } catch (e) {
      setSubmitErr(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete() {
    if (!initial) return;
    const confirmed = window.confirm(
      `Delete policy "${initial.name}"?\n\nThis is a soft-delete — the row stays in the database so historical violations remain attached. You can re-create with the same name to revive it.`,
    );
    if (!confirmed) return;
    setSubmitting(true);
    try {
      const res = await fetch(
        `${API_BASE}/v1/policies/${encodeURIComponent(initial.name)}`,
        { method: 'DELETE' },
      );
      if (!res.ok && res.status !== 204) {
        const err = await safeError(res);
        throw new Error(err);
      }
      router.push('/policies');
      router.refresh();
    } catch (e) {
      setSubmitErr(String((e as Error).message ?? e));
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6 max-w-3xl">
      <div className="card p-5 space-y-4">
        <Field
          label="Name"
          hint="Used as the deduplication key + shown on every violation."
        >
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={mode === 'edit'}
            required
            className="form-input font-mono"
            placeholder="e.g. cost_runaway"
          />
        </Field>
        <Field
          label="Description"
          hint="Shown on the violation row and in webhook payloads."
        >
          <input
            value={description ?? ''}
            onChange={(e) => setDescription(e.target.value)}
            className="form-input"
            placeholder="What does this catch and why does it matter?"
          />
        </Field>
      </div>

      <div className="card p-5 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Field label="Trigger" hint="When the engine evaluates this rule.">
            <select
              value={trigger}
              onChange={(e) => setTrigger(e.target.value as 'span_end' | 'trace_end')}
              className="form-input"
            >
              <option value="span_end">span_end (per LLM/tool call)</option>
              <option value="trace_end">trace_end (per agent run)</option>
            </select>
          </Field>
          <Field label="Severity" hint="Controls the badge color in the UI.">
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value as PolicySeverity)}
              className="form-input"
            >
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="critical">critical</option>
            </select>
          </Field>
          <Field label="Action" hint="alert posts to webhook; flag is silent.">
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as 'flag' | 'alert')}
              className="form-input"
            >
              <option value="flag">flag</option>
              <option value="alert">alert</option>
            </select>
          </Field>
        </div>

        <Field
          label="Condition"
          hint="A simpleeval expression. References span.* (span_end) or trace.* (trace_end). Available functions: len, str, int, float, abs."
        >
          <textarea
            value={condition}
            onChange={(e) => setCondition(e.target.value)}
            required
            rows={3}
            className="form-input font-mono text-sm"
            placeholder={
              trigger === 'span_end'
                ? "span.type == 'llm' and span.duration_ms > 15000"
                : 'trace.total_cost_usd > 0.50'
            }
          />
        </Field>
        {conditionWarn ? (
          <div className="text-amber-300 text-xs">⚠ {conditionWarn}</div>
        ) : null}
      </div>

      <div className="card p-5 space-y-4">
        <Field
          label="Scope to specific agents"
          hint="Comma-separated trace.name values. Leave empty to apply to every agent."
        >
          <input
            value={scopeAgents}
            onChange={(e) => setScopeAgents(e.target.value)}
            className="form-input font-mono"
            placeholder="e.g. customer_support_agent, billing_bot"
          />
        </Field>
        <Field
          label="Webhook URL (optional)"
          hint="If set + action=alert, the SDK POSTs the violation payload here. Falls back to lumin.configure(alert_webhook=…)."
        >
          <input
            value={webhookUrl ?? ''}
            onChange={(e) => setWebhookUrl(e.target.value)}
            className="form-input font-mono"
            placeholder="https://hooks.slack.com/services/…"
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="form-checkbox"
          />
          <span>Enabled</span>
          <span className="text-[var(--muted)] text-xs">
            (uncheck to soft-disable without deleting)
          </span>
        </label>
      </div>

      {submitErr ? (
        <div className="card p-3 text-rose-400 text-sm">{submitErr}</div>
      ) : null}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={submitting}
          className="pill pill-active disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? 'Saving…' : mode === 'create' ? 'Create policy' : 'Save changes'}
        </button>
        <Link href="/policies" className="pill">
          Cancel
        </Link>
        {mode === 'edit' && initial ? (
          <button
            type="button"
            onClick={handleDelete}
            disabled={submitting}
            className="pill text-rose-400 hover:text-rose-300 ml-auto disabled:opacity-50"
          >
            Delete policy
          </button>
        ) : null}
      </div>
    </form>
  );
}


function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
        {label}
      </div>
      {children}
      {hint ? (
        <div className="text-[11px] text-[var(--muted)] mt-1">{hint}</div>
      ) : null}
    </div>
  );
}


async function safeError(res: Response): Promise<string> {
  try {
    const j = await res.json();
    if (j?.detail) return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
  } catch {
    /* fall through */
  }
  return `HTTP ${res.status} ${res.statusText}`;
}


/** Quick client-side check for the most common typos. The server's
 * SimpleEval validation is the source of truth — this just catches
 * the obvious before submit. */
function localConditionLint(condition: string, trigger: string): string | null {
  if (!condition.trim()) return null;
  const expectedPrefix = trigger === 'span_end' ? 'span' : 'trace';
  const wrongPrefix = trigger === 'span_end' ? 'trace' : 'span';
  if (
    condition.includes(`${wrongPrefix}.`) &&
    !condition.includes(`${expectedPrefix}.`)
  ) {
    return `Condition references ${wrongPrefix}.* but trigger is ${trigger}. Did you mean to switch the trigger?`;
  }
  return null;
}
