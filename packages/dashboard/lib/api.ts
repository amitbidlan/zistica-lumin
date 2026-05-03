// Default base goes through the Next.js rewrite (see next.config.mjs)
// so the browser sees a same-origin request and no CORS is required.
export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? '/api';

export type Trace = {
  id: string;
  name: string | null;
  input: string | null;
  output: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  total_tokens: number;
  total_cost_usd: number;
  quality_score: number | null;
  user_id: string;
  session_id: string | null;
  tags: string[] | null;
  metadata: unknown;
  ingest_at: string | null;
};

export type Session = {
  session_id: string;
  trace_count: number;
  total_duration_ms: number;
  total_cost_usd: number;
  total_tokens: number;
  quality_score: number | null;
  first_seen: string | null;
  last_seen: string | null;
  wall_duration_ms: number | null;
};

export type SessionDetail = Session & {
  traces: Trace[];
};

export type Span = {
  id: string;
  trace_id: string;
  parent_span_id: string | null;
  type: string | null;
  name: string | null;
  input: string | null;
  output: string | null;
  model: string | null;
  provider: string | null;
  tokens_input: number | null;
  tokens_output: number | null;
  cost_usd: number | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  status: string;
  error_message: string | null;
  tool_name: string | null;
  metadata: unknown;
  span_subtype: 'thinking' | 'response' | null;
  thinking_tokens: number | null;
};

export const fetcher = async (path: string) => {
  const res = await fetch(API_BASE + path);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }
  return res.json();
};

export function formatCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return '—';
  if (usd === 0) return '$0';
  if (usd < 0.01) return `$${usd.toFixed(6)}`;
  return `$${usd.toFixed(4)}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return '—';
  return score.toFixed(2);
}

export function formatStartedAt(ts: string | null | undefined): string {
  if (!ts) return '—';
  // ISO without TZ from API; treat as UTC for display.
  const d = new Date(ts.includes('Z') || ts.includes('+') ? ts : ts + 'Z');
  return d.toLocaleString();
}

export function deriveStatus(t: Trace): 'ok' | 'running' {
  return t.ended_at ? 'ok' : 'running';
}
