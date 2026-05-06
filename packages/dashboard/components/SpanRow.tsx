'use client';

import { useState } from 'react';
import { Span, formatCost, formatDuration } from '@/lib/api';

const TYPE_COLORS: Record<string, string> = {
  llm: 'text-violet-300 border-violet-800/60',
  tool: 'text-cyan-300 border-cyan-800/60',
  retrieval: 'text-amber-300 border-amber-800/60',
  memory: 'text-pink-300 border-pink-800/60',
  custom: 'text-slate-300 border-slate-700',
};

function prettyJson(s: string | null): string {
  if (s === null || s === undefined || s === '') return '(empty)';
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch {
    return s;
  }
}

function thinkingText(input: string | null): string {
  // Thinking spans store {"thinking": "..."} in input. Strip the
  // wrapper so the dashboard shows the reasoning text directly.
  if (!input) return '';
  try {
    const parsed = JSON.parse(input);
    if (parsed && typeof parsed === 'object' && typeof parsed.thinking === 'string') {
      return parsed.thinking;
    }
  } catch {}
  return input;
}

/**
 * Pull a model's reasoning trace out of span metadata. Two integrations
 * use this pattern:
 *   - @lumin-io/openclaw-diagnostics (typed-hook plugin) writes the
 *     full thinking blocks to ``openclaw.content.thinking``.
 *   - Future framework adapters can use ``content.thinking`` as a
 *     conventional key — we look for both shapes.
 *
 * Distinct from the dedicated "thinking" sub-span emitted by the
 * Anthropic SDK integration (handled above via ``span_subtype ===
 * 'thinking'``). Reasoning models that don't emit separate spans
 * still get their reasoning surfaced.
 */
function metadataThinking(metadata: unknown): string | null {
  if (!metadata || typeof metadata !== 'object') return null;
  const md = metadata as Record<string, unknown>;
  const candidates = [
    md['openclaw.content.thinking'],
    md['content.thinking'],
  ];
  for (const c of candidates) {
    if (typeof c === 'string' && c.length > 0) return c;
  }
  return null;
}

export default function SpanRow({
  span,
  depth,
}: {
  span: Span;
  depth: number;
}) {
  const isThinking = span.span_subtype === 'thinking';
  const isResponse = span.span_subtype === 'response';
  // Thinking rows are collapsed by default (the reasoning is long;
  // the user opts in to read it). Other rows are also collapsed by
  // default — we keep the behavior uniform.
  const [open, setOpen] = useState(false);
  const typeClass = TYPE_COLORS[span.type ?? 'custom'] ?? TYPE_COLORS.custom;
  const isError = span.status === 'error';

  const rowClass = isThinking
    ? 'w-full text-left px-3 py-2 transition-colors flex items-center gap-3 bg-violet-950/20 hover:bg-violet-950/40 border-l-2 border-violet-700'
    : 'w-full text-left px-3 py-2 hover:bg-[var(--background-hover)] transition-colors flex items-center gap-3';

  const subtypeBadge = isThinking ? (
    <span
      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded text-violet-200 border-violet-600 bg-violet-900/40"
      data-testid="thinking-badge"
    >
      thinking
    </span>
  ) : isResponse ? (
    <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded text-emerald-200 border-emerald-700 bg-emerald-900/30">
      response
    </span>
  ) : null;

  return (
    <div data-span-subtype={span.span_subtype ?? 'none'}>
      <button
        onClick={() => setOpen(!open)}
        className={rowClass}
        aria-expanded={open}
      >
        <span
          aria-hidden
          className="text-[var(--muted)] font-mono select-none"
          style={{ paddingLeft: `${depth * 20}px` }}
        >
          {isThinking ? '🧠' : depth === 0 ? '●' : '└─'}
        </span>
        <span className="font-mono truncate flex-1">
          {span.name ?? '(unnamed)'}
        </span>
        {subtypeBadge}
        <span
          className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded ${typeClass}`}
        >
          {span.type ?? 'custom'}
        </span>
        {span.model && (
          <span className="text-xs text-[var(--muted)] font-mono">
            {span.model}
          </span>
        )}
        {isThinking && span.thinking_tokens != null ? (
          <span
            className="text-xs text-violet-300 font-mono"
            data-testid="thinking-tokens"
          >
            ~{span.thinking_tokens} thinking tok
          </span>
        ) : (
          (span.tokens_input != null || span.tokens_output != null) && (
            <span className="text-xs text-[var(--muted)] font-mono">
              {span.tokens_input ?? 0}/{span.tokens_output ?? 0} tok
            </span>
          )
        )}
        {span.cost_usd != null && (
          <span className="text-xs text-[var(--muted)] font-mono">
            {formatCost(span.cost_usd)}
          </span>
        )}
        <span className="text-xs font-mono text-[var(--muted)] tabular-nums w-16 text-right">
          {formatDuration(span.duration_ms)}
        </span>
        {isError && (
          <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded text-red-300 border-red-700 bg-red-900/30">
            error
          </span>
        )}
      </button>
      {open && (
        <div
          className={`px-4 pb-3 pt-1 text-xs space-y-3 ${
            isThinking ? 'bg-violet-950/10 border-l-2 border-violet-800/40' : 'bg-[var(--background-raised)]'
          }`}
        >
          {isThinking ? (
            <Field
              label="Reasoning"
              value={thinkingText(span.input) || '(empty)'}
              accent="thinking"
            />
          ) : (
            <>
              {/* Reasoning trace from metadata (e.g. OpenClaw plugin's
                  openclaw.content.thinking field). Rendered ABOVE
                  Input so the operator sees WHY the model answered
                  before they see the prompt + reply. */}
              {(() => {
                const thinking = metadataThinking(span.metadata);
                return thinking ? (
                  <Field label="Reasoning" value={thinking} accent="thinking" />
                ) : null;
              })()}
              {span.input != null && span.input !== '' && (
                <Field label="Input" value={prettyJson(span.input)} />
              )}
            </>
          )}
          {!isThinking && span.output != null && span.output !== '' && (
            <Field label="Output" value={prettyJson(span.output)} />
          )}
          {span.error_message && (
            <Field label="Error" value={span.error_message} error />
          )}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  error = false,
  accent,
}: {
  label: string;
  value: string;
  error?: boolean;
  accent?: 'thinking';
}) {
  const labelClass = error
    ? 'text-red-400'
    : accent === 'thinking'
      ? 'text-violet-300'
      : 'text-[var(--muted)]';
  const preClass = error
    ? 'text-red-300'
    : accent === 'thinking'
      ? 'text-violet-100 border-violet-800/60 bg-violet-950/20'
      : '';
  return (
    <div>
      <div className={`text-[10px] uppercase tracking-wider mb-1 ${labelClass}`}>
        {label}
      </div>
      <pre
        className={`font-mono whitespace-pre-wrap break-all border border-[var(--border)] rounded p-2 ${preClass}`}
      >
        {value}
      </pre>
    </div>
  );
}
