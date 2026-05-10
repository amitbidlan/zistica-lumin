'use client';

import { useState, type CSSProperties } from 'react';
import { Span, formatCost, formatDuration } from '@/lib/api';

// Theme-aware span-type badge colors. Backed by ``--span-<type>-*``
// CSS vars in globals.css that flip per ``data-theme``. Pre-fix
// these were Tailwind 300/800-weight classes that worked only in
// dark mode and rendered as pale-on-pale in light mode.
const TYPE_STYLES: Record<string, { fg: string; border: string }> = {
  llm:       { fg: 'var(--span-llm-fg)',       border: 'var(--span-llm-border)' },
  tool:      { fg: 'var(--span-tool-fg)',      border: 'var(--span-tool-border)' },
  retrieval: { fg: 'var(--span-retrieval-fg)', border: 'var(--span-retrieval-border)' },
  memory:    { fg: 'var(--span-memory-fg)',    border: 'var(--span-memory-border)' },
  custom:    { fg: 'var(--span-custom-fg)',    border: 'var(--span-custom-border)' },
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
  const typeStyle = TYPE_STYLES[span.type ?? 'custom'] ?? TYPE_STYLES.custom;
  const isError = span.status === 'error';

  // Thinking-row chrome uses ``--thinking-*`` CSS vars so it stays
  // legible in both light and dark mode. Pre-2026-05-11 these were
  // hardcoded ``bg-violet-950/20``-style classes — fine in dark,
  // invisible-on-pale-violet in light. Same fix as the Field
  // component below.
  const rowClass = isThinking
    ? 'w-full text-left px-3 py-2 transition-colors flex items-center gap-3 border-l-2'
    : 'w-full text-left px-3 py-2 hover:bg-[var(--background-hover)] transition-colors flex items-center gap-3';
  const rowStyle: CSSProperties | undefined = isThinking
    ? {
        background: 'var(--thinking-bg)',
        borderLeftColor: 'var(--thinking-border)',
      }
    : undefined;

  const subtypeBadge = isThinking ? (
    <span
      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded"
      style={{
        color: 'var(--thinking-fg)',
        borderColor: 'var(--thinking-border)',
        background: 'var(--thinking-bg-hover)',
      }}
      data-testid="thinking-badge"
    >
      thinking
    </span>
  ) : isResponse ? (
    <span
      className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded"
      style={{
        color: 'var(--span-response-fg)',
        borderColor: 'var(--span-response-border)',
      }}
    >
      response
    </span>
  ) : null;

  return (
    <div data-span-subtype={span.span_subtype ?? 'none'}>
      <button
        onClick={() => setOpen(!open)}
        className={rowClass}
        style={rowStyle}
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
          className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded"
          style={{ color: typeStyle.fg, borderColor: typeStyle.border }}
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
            className="text-xs font-mono"
            style={{ color: 'var(--thinking-fg-soft)' }}
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
          <span
            className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded"
            style={{
              color: 'var(--span-error-fg)',
              background: 'var(--span-error-bg)',
              borderColor: 'var(--span-error-border)',
            }}
          >
            error
          </span>
        )}
      </button>
      {open && (
        <div
          className={`px-4 pb-3 pt-1 text-xs space-y-3 ${
            isThinking ? 'border-l-2' : 'bg-[var(--background-raised)]'
          }`}
          style={
            isThinking
              ? {
                  background: 'var(--thinking-bg)',
                  borderLeftColor: 'var(--thinking-border)',
                }
              : undefined
          }
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
  const isThinking = accent === 'thinking';
  // Light-mode bug fix (2026-05-11): the previous version used
  // hardcoded Tailwind violets (``text-violet-100`` on
  // ``bg-violet-950/20``). Those work in dark mode but are
  // invisible on a white background — pale lavender on white. Move
  // to the same ``--thinking-*`` CSS variables ChatView uses; they
  // flip per ``data-theme`` so reasoning is readable in both modes.
  const labelClass = isThinking || error
    ? '' // color via inline style below
    : 'text-[var(--muted)]';
  const preClass = '';
  const labelStyle: CSSProperties | undefined = error
    ? { color: 'var(--span-error-fg)' }
    : isThinking
      ? { color: 'var(--thinking-fg-soft)' }
      : undefined;
  const preStyle: CSSProperties | undefined = error
    ? {
        color: 'var(--span-error-fg)',
        background: 'var(--span-error-bg)',
        borderColor: 'var(--span-error-border)',
      }
    : isThinking
      ? {
          color: 'var(--thinking-fg)',
          background: 'var(--thinking-bg)',
          borderColor: 'var(--thinking-border)',
        }
      : undefined;
  return (
    <div>
      <div
        className={`text-[10px] uppercase tracking-wider mb-1 ${labelClass}`}
        style={labelStyle}
      >
        {label}
      </div>
      <pre
        className={`font-mono whitespace-pre-wrap break-all border ${isThinking || error ? '' : 'border-[var(--border)]'} rounded p-2 ${preClass}`}
        style={preStyle}
      >
        {value}
      </pre>
    </div>
  );
}
