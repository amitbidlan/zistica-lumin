'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';

import {
  DecisionDetailResponse,
  DecisionRow,
  fetchDecisionDetail,
  postLabel,
} from '@/lib/api';

import { DecisionBadge } from './EnforcementTimeline';

/**
 * "Why was this blocked?" view (§5.3 / §8.2). Renders a single
 * decision plus:
 *   - the matched policy snapshot (current YAML/DB shape)
 *   - sibling decisions on the same trace (cross-policy ordering)
 *   - the matched value (truncated to 200 chars by the API)
 *
 * The detail page at /decisions/[id] hosts this; it can also be
 * mounted from a trace detail view to surface the firewall context
 * inline.
 */
export default function DecisionDetailPanel({ decisionId }: { decisionId: string }) {
  const { data, error, isLoading } = useSWR<DecisionDetailResponse>(
    `decision:${decisionId}`,
    () => fetchDecisionDetail(decisionId),
  );

  if (error) {
    return (
      <div className="card p-6 text-rose-400">
        Failed to load decision: {String(error.message ?? error)}
      </div>
    );
  }
  if (isLoading || !data) {
    return <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>;
  }

  const d = data.decision;
  return (
    <div className="space-y-6">
      <div className="card p-5 space-y-4">
        <div className="flex items-center gap-3 flex-wrap">
          <DecisionBadge decision={d.decision} mode={d.mode_at_decision} />
          <span className="text-sm text-[var(--muted)]">{d.lifecycle}</span>
          <span className="text-sm text-[var(--muted)]">·</span>
          <span className="text-sm text-[var(--muted)]">{d.duration_ms}ms</span>
          <span className="text-sm text-[var(--muted)]">·</span>
          <span className="text-sm text-[var(--muted)]">{d.decision_at}</span>
        </div>

        <h2 className="text-xl font-semibold">{d.policy_name}</h2>

        {d.reason && (
          <p className="text-sm text-[var(--foreground-soft)]">{d.reason}</p>
        )}

        <DefList
          rows={[
            ['Trace', d.trace_id ? <TraceLink id={d.trace_id} /> : '—'],
            ['Span', d.span_id || '—'],
            ['Session', d.session_id || '—'],
            ['Agent', d.agent || '—'],
            ['Project', d.project || '—'],
            ['Tool', d.tool_name || '—'],
            ['Matched field', d.matched_field || '—'],
          ]}
        />

        {d.matched_value_truncated && (
          <div>
            <div className="text-xs uppercase tracking-wide text-[var(--muted)] mb-1">
              Offending value
            </div>
            <pre className="text-xs bg-[var(--surface-elev)] p-3 rounded border border-[var(--border)] overflow-x-auto whitespace-pre-wrap">
              {d.matched_value_truncated}
            </pre>
          </div>
        )}

        {/* Slice 3 PR C — operator actions on the decision. False
            positive marking feeds the local-classifier trainer
            (Slice 4); "Use a template" is the recommended path for
            authoring a new rule from observed bad behavior. */}
        <DecisionActions decision={d} />
      </div>

      {data.policy && (
        <div className="card p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-semibold">Matched policy</h3>
            <Link
              href={`/policies/${encodeURIComponent(data.policy.name)}`}
              className="text-xs text-[var(--accent)] hover:underline"
            >
              Open editor →
            </Link>
          </div>
          {data.policy.description && (
            <p className="text-sm text-[var(--foreground-soft)]">{data.policy.description}</p>
          )}
          <DefList
            rows={[
              ['Lifecycle', data.policy.lifecycle],
              ['Mode', data.policy.mode],
              ['Action', data.policy.action],
              ['Severity', data.policy.severity],
              ['Priority', String(data.policy.priority)],
            ]}
          />
          <div>
            <div className="text-xs uppercase tracking-wide text-[var(--muted)] mb-1">
              Condition
            </div>
            <pre className="text-xs bg-[var(--surface-elev)] p-3 rounded border border-[var(--border)] overflow-x-auto">
              {data.policy.condition}
            </pre>
          </div>
        </div>
      )}

      {data.siblings.length > 0 && (
        <div className="card p-5 space-y-3">
          <h3 className="text-lg font-semibold">
            Other decisions on this trace ({data.siblings.length})
          </h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-[var(--muted)] border-b border-[var(--border)]">
                <th className="py-2">When</th>
                <th className="py-2">Decision</th>
                <th className="py-2">Lifecycle</th>
                <th className="py-2">Policy</th>
              </tr>
            </thead>
            <tbody>
              {data.siblings.map((sib) => (
                <tr key={sib.id} className="border-b border-[var(--border)] last:border-0">
                  <td className="py-2 text-[var(--muted)]">{sib.decision_at}</td>
                  <td className="py-2">
                    <DecisionBadge decision={sib.decision} mode={sib.mode_at_decision} />
                  </td>
                  <td className="py-2 text-[var(--muted)]">{sib.lifecycle}</td>
                  <td className="py-2">
                    <Link
                      href={`/decisions/${sib.id}`}
                      className="hover:underline"
                    >
                      {sib.policy_name}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}


function DefList({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return (
    <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="flex gap-2">
          <dt className="w-32 shrink-0 text-[var(--muted)]">{k}</dt>
          <dd className="font-medium break-all">{v}</dd>
        </div>
      ))}
    </dl>
  );
}


function TraceLink({ id }: { id: string }) {
  return (
    <Link
      href={`/traces/${id}`}
      className="font-mono text-xs hover:underline text-[var(--accent)]"
    >
      {id.slice(0, 8)}…{id.slice(-4)}
    </Link>
  );
}


function DecisionActions({ decision }: { decision: DecisionRow }) {
  const [labeling, setLabeling] = useState(false);
  const [labeled, setLabeled] = useState(false);
  const [labelError, setLabelError] = useState<string | null>(null);

  async function markFalsePositive() {
    if (!decision.trace_id) {
      setLabelError('No trace_id on this decision — cannot label');
      return;
    }
    setLabeling(true);
    setLabelError(null);
    try {
      await postLabel({
        trace_id: decision.trace_id,
        span_id: decision.span_id ?? undefined,
        decision_id: decision.id,
        field:
          decision.lifecycle === 'before_tool_call' ||
          decision.lifecycle === 'after_tool_call'
            ? 'tool_params'
            : 'output',
        label: 'good',
        category: 'false_positive',
        notes: `Marked false-positive for policy ${decision.policy_name} from /decisions/${decision.id}`,
      });
      setLabeled(true);
    } catch (e) {
      setLabelError((e as Error).message);
    } finally {
      setLabeling(false);
    }
  }

  return (
    <div className="border-t border-[var(--border)] pt-4 flex items-center gap-2 flex-wrap">
      <Link
        href="/templates"
        className="px-3 py-1.5 rounded text-xs border border-[var(--border)] hover:bg-[var(--surface-hover)] transition-colors"
        title="Author a new rule from a pre-built template"
      >
        + New rule from template
      </Link>

      {labeled ? (
        <span className="text-xs text-emerald-400 px-3 py-1.5">
          ✓ Marked as false positive
        </span>
      ) : (
        <button
          onClick={markFalsePositive}
          disabled={labeling}
          className="px-3 py-1.5 rounded text-xs border border-[var(--border)] hover:bg-[var(--surface-hover)] transition-colors disabled:opacity-50"
          title="Records a 'good' label so this trace doesn't trip the rule next time the local classifier retrains"
        >
          {labeling ? 'Marking…' : 'Mark as false positive'}
        </button>
      )}

      {labelError ? (
        <span className="text-xs text-rose-400">{labelError}</span>
      ) : null}
    </div>
  );
}
