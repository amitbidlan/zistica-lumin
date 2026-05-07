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
  // Policy Engine — list endpoint includes a count for the badge;
  // detail endpoint adds the full per-trace violation summary.
  // All default to 0/empty/false when the engine isn't in use.
  violation_count: number;
  policy_violations?: TraceViolationSummary[];
  has_violations?: boolean;
};

export type TraceViolationSummary = {
  policy_name: string;
  severity: PolicySeverity;
};

export type PolicySeverity = 'low' | 'medium' | 'high' | 'critical';

export type PolicyViolation = {
  id: string;
  policy_name: string;
  policy_description: string | null;
  severity: PolicySeverity;
  trace_id: string;
  span_id: string | null;
  condition_text: string | null;
  action_taken: string | null;
  actual_value: string | null;
  webhook_fired: boolean;
  webhook_url: string | null;
  created_at: string | null;
};

export type PolicyViolationsResponse = {
  violations: PolicyViolation[];
  total: number;
};

export type PolicyViolationStats = {
  total: number;
  by_severity: Record<string, number>;
  by_policy: Record<string, number>;
};

export type ActivityLabel = 'active' | 'idle' | 'dormant';

export type AgentSummary = {
  name: string;
  project: string;       // openclaw / mastra / voltagent / default
  trace_count: number;
  total_cost_usd: number;
  total_tokens: number;
  avg_duration_ms: number;
  error_rate: number;
  violation_count: number;
  has_violations: boolean;
  last_seen: string | null;
  top_model: string | null;
  top_provider: string | null;
  providers: string[];   // distinct LLM providers used
  seconds_since_last_seen: number | null;
  activity: ActivityLabel;
  // Phase 2 — in-flight count + sparkline data
  active_traces: number;
  activity_buckets: number[];  // 12 ints: bucket 0 = most recent 5min
};

export type AgentListResponse = {
  agents: AgentSummary[];
  window_hours: number;
  projects: string[];    // distinct projects in the response
  // True when the DB has traces older than the current window — the
  // empty-state shows a "try 7d filter" hint to bridge the asymmetry
  // between /traces (no window) and /agents (24h default).
  older_data_exists: boolean;
};

export type AgentDetail = AgentSummary & {
  recent_traces: Trace[];
  violations_by_policy: Record<string, number>;
  violations_by_severity: Record<string, number>;
};

export function formatActivity(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'no activity';
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

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

// ---- Policies (Phase 3 read, Phase 4 write) -----------------------------

export type PolicyTrigger = 'span_end' | 'trace_end';
// Action vocabulary covers both layers: legacy post-ingest (flag,
// alert) + Agent Firewall (allow, block, require_approval, rewrite).
// Editor and decision-row renderers branch on this union.
export type PolicyAction =
  | 'flag'
  | 'alert'
  | 'allow'
  | 'block'
  | 'require_approval'
  | 'rewrite';

export type FirewallLifecycle =
  | 'post_ingest'
  | 'before_proxy_call'
  | 'after_proxy_call'
  | 'before_tool_call'
  | 'after_tool_call';

export type FirewallMode = 'shadow' | 'flag' | 'enforce';

export type FirewallDecisionVerb =
  | 'allow'
  | 'block'
  | 'flag'
  | 'require_approval'
  | 'rewrite';

export type Policy = {
  name: string;
  description: string | null;
  trigger: PolicyTrigger;
  condition: string;
  action: PolicyAction;
  severity: PolicySeverity;
  webhook_url: string | null;
  scope_agents: string[];
  enabled: boolean;
  source: 'yaml' | 'db' | 'none';
  version: number;
  // Agent Firewall fields. Optional on the wire — the API only emits
  // them when the firewall migration has run; treat absence as the
  // safe default ("post_ingest" / "enforce" for legacy rows, "shadow"
  // for newly-authored ones).
  lifecycle?: FirewallLifecycle;
  mode?: FirewallMode;
  priority?: number;
  on_timeout?: 'allow' | 'deny';
  on_internal_error?: 'allow' | 'deny' | 'flag';
};

export type PolicyListResponse = {
  policies: Policy[];
  source: 'yaml' | 'db' | 'none';
  engine_loaded: boolean;
};

export type PolicyAuditEntry = {
  id: string;
  policy_name: string;
  action: 'create' | 'update' | 'delete';
  before: unknown;
  after: unknown;
  actor: string | null;
  created_at: string | null;
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


// ---- Agent Firewall — decisions + approvals + panic ---------------------

export type DecisionRow = {
  id: string;
  policy_id: string;
  policy_name: string;
  lifecycle: FirewallLifecycle;
  decision: FirewallDecisionVerb;
  mode_at_decision: FirewallMode | string;
  reason: string | null;
  trace_id: string | null;
  span_id: string | null;
  session_id: string | null;
  agent: string | null;
  project: string | null;
  tool_name: string | null;
  matched_field: string | null;
  matched_value_truncated: string | null;
  decision_at: string;
  duration_ms: number;
  metadata: unknown;
};

export type DecisionsListResponse = {
  decisions: DecisionRow[];
  total: number;
  has_more: boolean;
};

export type DecisionDetailResponse = {
  decision: DecisionRow;
  policy: {
    name: string;
    description: string | null;
    lifecycle: FirewallLifecycle;
    mode: FirewallMode;
    action: PolicyAction;
    severity: PolicySeverity;
    condition: string;
    priority: number;
  } | null;
  siblings: DecisionRow[];
};

export type ModeChangeForecast = {
  would_have_blocked: number;
  examples: string[];
};

export type ModeChangeResponse = {
  id: string;
  mode: FirewallMode;
  previous_mode: FirewallMode | string;
  forecast: ModeChangeForecast;
};

export type ApprovalRow = {
  id: string;
  decision_id: string;
  policy_id: string;
  trace_id: string | null;
  agent: string | null;
  tool_name: string | null;
  params_truncated: unknown;
  state: 'pending' | 'allowed' | 'denied' | 'timed_out';
  requested_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_reason: string | null;
  timeout_at: string;
  on_timeout: 'allow' | 'deny';
};

export type ApprovalsListResponse = {
  approvals: ApprovalRow[];
  total: number;
};

export type PanicState = {
  disabled: boolean;
};


export async function fetchDecisions(params: {
  decision?: FirewallDecisionVerb;
  lifecycle?: FirewallLifecycle;
  agent?: string;
  trace_id?: string;
  session_id?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}): Promise<DecisionsListResponse> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  }
  const path = `/v1/decisions${q.toString() ? `?${q}` : ''}`;
  return fetcher(path);
}

export async function fetchDecisionDetail(id: string): Promise<DecisionDetailResponse> {
  return fetcher(`/v1/decisions/${encodeURIComponent(id)}`);
}

export async function setPolicyMode(
  name: string, mode: FirewallMode,
): Promise<ModeChangeResponse> {
  const res = await fetch(`${API_BASE}/v1/policies/${encodeURIComponent(name)}/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Mode change failed: HTTP ${res.status} ${text}`);
  }
  return res.json();
}

export async function fetchPanicState(): Promise<PanicState> {
  return fetcher(`/v1/firewall/panic_disable`);
}

export async function setPanicDisabled(
  disabled: boolean, reason?: string, actor?: string,
): Promise<{ disabled: boolean; reason: string | null; updated_at: string; updated_by: string | null }> {
  const res = await fetch(`${API_BASE}/v1/firewall/panic_disable`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disabled, reason, actor }),
  });
  if (!res.ok) {
    throw new Error(`Panic toggle failed: HTTP ${res.status}`);
  }
  return res.json();
}

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
