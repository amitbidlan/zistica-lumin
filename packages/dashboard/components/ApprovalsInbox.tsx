'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';

import {
  ApprovalRow,
  ApprovalsListResponse,
  fetchApprovals,
  resolveApproval,
} from '@/lib/api';

/**
 * Admin-facing inbox for pending firewall approvals (§5.6 / §5.7,
 * task 3.4). When a `require_approval` rule fires, the agent is
 * blocked until an operator says allow or deny — this is where they
 * say it.
 *
 * Cluster C separation: when admin senders are excluded from
 * chat-side approval prompts (so end users never see "approve this
 * `rm -rf`?"), this dashboard surface is the operator's path. The
 * agent that's long-polling /v1/approvals/{id} unblocks the moment
 * we resolve here.
 *
 * Polls every 3s — faster than the decisions timeline (5s) because
 * an agent is literally waiting on each row, and operator decision
 * latency is the user-visible metric. Empty state is a calm "all
 * clear" rather than a loading spinner — most of the time the
 * inbox should be empty.
 */
export default function ApprovalsInbox() {
  const { data, error, isLoading, mutate } = useSWR<ApprovalsListResponse>(
    'approvals:pending',
    () => fetchApprovals({ state: 'pending', limit: 50 }),
    { refreshInterval: 3_000 },
  );

  if (error) {
    return (
      <div className="card p-6 text-rose-400">
        Failed to load approvals: {String(error.message ?? error)}
      </div>
    );
  }

  if (isLoading || !data) {
    return (
      <div className="card p-8 text-center text-[var(--muted)]">Loading…</div>
    );
  }

  if (data.approvals.length === 0) {
    return (
      <div className="card p-10 text-center">
        <div className="text-2xl mb-2" aria-hidden>
          {'\u{1F389}'}
        </div>
        <div className="text-sm text-[var(--muted)]">
          No pending approvals.
        </div>
        <div className="text-xs text-[var(--muted)] mt-1 opacity-70">
          The dashboard is polling every 3s — new requests will appear
          automatically.
        </div>
      </div>
    );
  }

  return (
    <div className="card overflow-hidden">
      <div className="px-4 py-3 border-b border-[var(--border)] flex items-center justify-between">
        <span className="text-sm font-medium">Pending approvals</span>
        <span
          className="inline-flex items-center justify-center min-w-[1.5rem] px-2 py-0.5 rounded-full
                     text-xs font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/40"
        >
          {data.approvals.length}
        </span>
      </div>
      <ul className="divide-y divide-[var(--border)]">
        {data.approvals.map((a) => (
          <ApprovalListItem
            key={a.id}
            approval={a}
            onResolved={async () => {
              await mutate();
            }}
          />
        ))}
      </ul>
    </div>
  );
}


type Mode = { kind: 'idle' } | { kind: 'confirm'; resolution: 'allow' | 'deny' };

function ApprovalListItem({
  approval,
  onResolved,
}: {
  approval: ApprovalRow;
  onResolved: () => void | Promise<void>;
}) {
  const [mode, setMode] = useState<Mode>({ kind: 'idle' });
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  async function submit(resolution: 'allow' | 'deny') {
    setSubmitting(true);
    setErrorText(null);
    try {
      await resolveApproval(
        approval.id,
        resolution,
        reason.trim() || undefined,
        'dashboard',
      );
      // Re-fetch — row should disappear from `state=pending`.
      await onResolved();
    } catch (e) {
      setErrorText(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  }

  // Format JSON params, max ~200 chars on a single line of code.
  const paramsText = formatParams(approval.params_truncated);

  return (
    <li className="px-4 py-3 hover:bg-[var(--surface-hover)]">
      <div className="flex items-start gap-3">
        <span
          className="mt-0.5 text-amber-400 select-none"
          aria-hidden
          title="awaiting approval"
        >
          {'⚠'}
        </span>
        <div className="flex-1 min-w-0">
          <div className="font-medium text-sm break-all">
            {approval.policy_id}
          </div>

          <div className="text-xs text-[var(--muted)] mt-0.5 flex items-center gap-2 flex-wrap">
            {approval.tool_name && (
              <span>
                tool: <span className="font-mono">{approval.tool_name}</span>
              </span>
            )}
            {approval.tool_name && approval.agent && <span aria-hidden>·</span>}
            {approval.agent && (
              <span>
                agent: <span className="font-mono">{approval.agent}</span>
              </span>
            )}
          </div>

          {paramsText && (
            <pre
              className="mt-2 text-xs bg-[var(--surface-elev)] p-2 rounded
                         border border-[var(--border)] overflow-x-auto
                         whitespace-pre-wrap break-all"
            >
              {paramsText}
            </pre>
          )}

          <div className="text-xs text-[var(--muted)] mt-2 flex items-center gap-2 flex-wrap">
            {approval.trace_id && (
              <>
                <Link
                  href={`/traces/${approval.trace_id}`}
                  className="font-mono hover:underline text-[var(--accent)]"
                >
                  trace {approval.trace_id.slice(0, 8)}…
                </Link>
                <span aria-hidden>·</span>
              </>
            )}
            <span>{formatRelative(approval.requested_at)}</span>
            {approval.timeout_at && (
              <>
                <span aria-hidden>·</span>
                <span title={`auto-${approval.on_timeout} at ${approval.timeout_at}`}>
                  times out {formatRelative(approval.timeout_at)} (
                  {approval.on_timeout})
                </span>
              </>
            )}
          </div>

          {/* Action row */}
          <div className="mt-3">
            {mode.kind === 'idle' ? (
              <div className="flex items-center gap-2">
                <button
                  onClick={() =>
                    setMode({ kind: 'confirm', resolution: 'allow' })
                  }
                  className="px-3 py-1 rounded text-xs font-medium border
                             bg-emerald-500/10 hover:bg-emerald-500/20
                             text-emerald-300 border-emerald-500/40"
                >
                  Allow
                </button>
                <button
                  onClick={() =>
                    setMode({ kind: 'confirm', resolution: 'deny' })
                  }
                  className="px-3 py-1 rounded text-xs font-medium border
                             bg-rose-500/10 hover:bg-rose-500/20
                             text-rose-300 border-rose-500/40"
                >
                  Deny
                </button>
              </div>
            ) : (
              <div className="space-y-2">
                <input
                  type="text"
                  placeholder="Reason (optional)"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  disabled={submitting}
                  autoFocus
                  className="w-full px-2 py-1 rounded border bg-transparent
                             text-xs border-[var(--border)]
                             placeholder:text-[var(--muted)]
                             focus:outline-none focus:border-[var(--accent)]"
                />
                <div className="flex items-center gap-2">
                  <span className="text-xs text-[var(--muted)] mr-1">
                    {mode.resolution === 'allow' ? 'Allow this?' : 'Deny this?'}
                  </span>
                  <button
                    onClick={() => submit(mode.resolution)}
                    disabled={submitting}
                    className={
                      'px-3 py-1 rounded text-xs font-medium border ' +
                      (mode.resolution === 'allow'
                        ? 'bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 border-emerald-500/50'
                        : 'bg-rose-500/20 hover:bg-rose-500/30 text-rose-200 border-rose-500/50') +
                      ' disabled:opacity-50'
                    }
                  >
                    {submitting ? 'Submitting…' : 'Confirm'}
                  </button>
                  <button
                    onClick={() => {
                      setMode({ kind: 'idle' });
                      setReason('');
                      setErrorText(null);
                    }}
                    disabled={submitting}
                    className="px-3 py-1 rounded text-xs font-medium border
                               border-[var(--border)] text-[var(--muted)]
                               hover:text-[var(--foreground)] disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
                {errorText && (
                  <div className="text-xs text-rose-400">{errorText}</div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </li>
  );
}


function formatParams(params: unknown): string | null {
  if (params === null || params === undefined) return null;
  let text: string;
  try {
    text = typeof params === 'string' ? params : JSON.stringify(params, null, 2);
  } catch {
    text = String(params);
  }
  if (!text) return null;
  if (text.length > 400) {
    return text.slice(0, 400) + '…';
  }
  return text;
}


function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  // Naive timestamps from DuckDB → treat as UTC.
  const d = new Date(iso.includes('Z') || iso.includes('+') ? iso : `${iso}Z`);
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec >= 0) {
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return d.toLocaleDateString();
  }
  // Future timestamp (e.g. timeout_at) → "in X".
  const future = -sec;
  if (future < 60) return `in ${future}s`;
  if (future < 3600) return `in ${Math.floor(future / 60)}m`;
  if (future < 86400) return `in ${Math.floor(future / 3600)}h`;
  return d.toLocaleString();
}
