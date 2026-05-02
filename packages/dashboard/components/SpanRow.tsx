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

export default function SpanRow({
  span,
  depth,
}: {
  span: Span;
  depth: number;
}) {
  const [open, setOpen] = useState(false);
  const typeClass = TYPE_COLORS[span.type ?? 'custom'] ?? TYPE_COLORS.custom;
  const isError = span.status === 'error';

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full text-left px-3 py-2 hover:bg-[#111418] transition-colors flex items-center gap-3"
        aria-expanded={open}
      >
        <span
          aria-hidden
          className="text-[var(--muted)] font-mono select-none"
          style={{ paddingLeft: `${depth * 20}px` }}
        >
          {depth === 0 ? '●' : '└─'}
        </span>
        <span className="font-mono truncate flex-1">
          {span.name ?? '(unnamed)'}
        </span>
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
        {span.tokens_input != null && (
          <span className="text-xs text-[var(--muted)] font-mono">
            {span.tokens_input}/{span.tokens_output ?? 0} tok
          </span>
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
        <div className="px-4 pb-3 pt-1 bg-[#0d1014] text-xs space-y-3">
          <Field label="Input" value={prettyJson(span.input)} />
          <Field label="Output" value={prettyJson(span.output)} />
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
}: {
  label: string;
  value: string;
  error?: boolean;
}) {
  return (
    <div>
      <div
        className={`text-[10px] uppercase tracking-wider mb-1 ${
          error ? 'text-red-400' : 'text-[var(--muted)]'
        }`}
      >
        {label}
      </div>
      <pre
        className={`font-mono whitespace-pre-wrap break-all border border-[var(--border)] rounded p-2 ${
          error ? 'text-red-300' : ''
        }`}
      >
        {value}
      </pre>
    </div>
  );
}
