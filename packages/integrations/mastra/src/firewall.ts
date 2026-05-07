/**
 * @lumin-io/mastra — Agent Firewall integration (Slice 3 PR J).
 *
 * Provides synchronous firewall enforcement for Mastra tools at parity
 * with the OpenClaw v0.4.0 plugin: every tool call hits Lumin's
 * /v1/policy/decide before executing, and the response decides whether
 * the tool runs, gets rewritten, requires operator approval, or is
 * blocked outright.
 *
 * Mastra and OpenClaw are different frameworks with different plugin
 * APIs, so the integration model differs:
 *
 *   - **OpenClaw** registers a single plugin that subscribes to typed
 *     hooks (``before_tool_call``, ``before_message_write``, etc.)
 *   - **Mastra** is class-based — operators wrap their tools with
 *     ``wrapToolWithFirewall(tool, cfg)``; the wrapper handles the
 *     decide round-trip + approval polling.
 *
 * Feature parity with the OpenClaw v0.4.0 plugin:
 *   - Synchronous decide on every tool call (Slice 1 §5.1)
 *   - block / rewrite / require_approval / allow translation (§5.4)
 *   - Approval long-poll with timeout (§5.7)
 *   - Admin sender separation (§Slice 2 Tier 1.0)
 *   - agent_feedback string consumption (§Slice 2 Tier 1.5)
 *   - Fail-mode config (allow vs deny) — Rule 7
 */

const DEFAULT_HOST = 'http://localhost:8000';
const DEFAULT_PROJECT = 'mastra';
const DEFAULT_DECIDE_TIMEOUT_MS = 75;
const DEFAULT_APPROVAL_TIMEOUT_MS = 10 * 60 * 1000;

export type DecideLifecycle =
  | 'before_proxy_call'
  | 'after_proxy_call'
  | 'before_tool_call'
  | 'after_tool_call'
  | 'post_ingest';

export type DecisionVerb =
  | 'allow'
  | 'block'
  | 'flag'
  | 'require_approval'
  | 'rewrite';

export interface DecideRequestBody {
  lifecycle: DecideLifecycle;
  tool_name?: string;
  params?: Record<string, unknown>;
  trace_id?: string;
  span_id?: string;
  session_id?: string;
  agent?: string;
  project?: string;
  model?: string;
  output?: unknown;
  /** Sender-id passthrough — Lumin uses this for per-sender rate
   * limiting + admin-aware decision policies. Not required. */
  sender_id?: string;
}

export interface DecideResponseBody {
  decision: DecisionVerb;
  policy_id?: string;
  policy_name?: string;
  reason?: string;
  decision_id?: string;
  mode_at_decision?: string;
  duration_ms?: number;
  approval_id?: string;
  timeout_s?: number;
  rewritten?: { params?: Record<string, unknown>; result?: unknown };
  /** Slice 2 Tier 1.5: when the decision is block / require_approval
   * the API returns a short string that the operator wants surfaced
   * to the LLM (e.g. "tool blocked: don't fabricate a /approve
   * surface; ask the user to contact the operator instead"). The
   * Mastra wrapper splices this into the tool's error result so
   * the next LLM turn sees it. */
  agent_feedback?: string;
}

export interface FirewallConfig {
  host?: string;
  apiKey?: string;
  project?: string;
  /** Per-call timeout for /v1/policy/decide. Tighter than trace
   * ingest because every tool call pays this latency. Defaults to
   * 75ms — fits §2.4 budget on a localhost API. */
  decideTimeoutMs?: number;
  /** What happens when the decide endpoint is unreachable / slow.
   * "allow" (default, Rule 7) keeps the tool flowing; "deny" stops
   * it. Production deployments wanting fail-closed flip this. */
  onFirewallError?: 'allow' | 'deny';
  /** Approval long-poll cap. After this, the wrapper resolves as
   * if the operator denied — the original spec §5.7 contract. */
  approvalTimeoutMs?: number;
  /** Admin sender ids. When a non-admin's call is blocked, the
   * wrapper substitutes ``userBlockedMessage`` for the LLM-bound
   * error message, closing the social-engineering surface where
   * the LLM hallucinates a fake /approve prompt the user can
   * click. Empty list = every sender is non-admin (most
   * conservative). */
  adminSenders?: string[];
  userBlockedMessage?: string;
  /** When false, even admins get the canned message instead of
   * the LLM's full reasoning. Defaults to true. */
  adminSeesFullResponse?: boolean;
  /** Override the global fetch (useful for tests). */
  fetchImpl?: typeof fetch;
}

const DEFAULT_USER_BLOCKED_MESSAGE =
  "I'm unable to perform that action due to security policy. " +
  'Please contact your administrator if you need assistance.';


export class LuminFirewallClient {
  private host: string;
  private project: string;
  private apiKey?: string;
  private timeoutMs: number;
  private approvalTimeoutMs: number;
  private onError: 'allow' | 'deny';
  private fetchImpl: typeof fetch;
  private failureLogged = false;

  constructor(cfg: FirewallConfig = {}) {
    this.host = (cfg.host || DEFAULT_HOST).replace(/\/+$/, '');
    this.project = cfg.project || DEFAULT_PROJECT;
    this.apiKey = cfg.apiKey;
    this.timeoutMs = cfg.decideTimeoutMs ?? DEFAULT_DECIDE_TIMEOUT_MS;
    this.approvalTimeoutMs =
      cfg.approvalTimeoutMs ?? DEFAULT_APPROVAL_TIMEOUT_MS;
    this.onError = cfg.onFirewallError ?? 'allow';
    this.fetchImpl = cfg.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  /** Resolve a decision. Never throws — on error/timeout returns the
   * configured fail-mode response (Rule 7). */
  async decide(body: DecideRequestBody): Promise<DecideResponseBody> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const resp = await this.fetchImpl(`${this.host}/v1/policy/decide`, {
        method: 'POST',
        headers: this._headers(),
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!resp.ok) {
        return this._failResponse(`http_${resp.status}`);
      }
      return (await resp.json()) as DecideResponseBody;
    } catch (err) {
      if (!this.failureLogged) {
        // eslint-disable-next-line no-console
        console.warn(
          `[lumin-mastra] firewall decide failed (${(err as Error).message}); ` +
            `applying onFirewallError=${this.onError}. Further failures suppressed.`,
        );
        this.failureLogged = true;
      }
      return this._failResponse(`error:${(err as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
  }

  /** Long-poll an approval until it resolves or times out. */
  async waitForApproval(
    approvalId: string,
    timeoutMs?: number,
  ): Promise<'allowed' | 'denied' | 'timed_out'> {
    const deadline = Date.now() + (timeoutMs ?? this.approvalTimeoutMs);
    let interval = 200;
    while (Date.now() < deadline) {
      try {
        const resp = await this.fetchImpl(
          `${this.host}/v1/approvals/${encodeURIComponent(approvalId)}`,
          { method: 'GET', headers: this._headers() },
        );
        if (resp.ok) {
          const body = (await resp.json()) as { state?: string };
          if (body.state === 'allowed') return 'allowed';
          if (body.state === 'denied') return 'denied';
          if (body.state === 'timed_out') return 'timed_out';
        }
      } catch {
        // ignore + retry until the deadline
      }
      await this._sleep(interval);
      interval = Math.min(interval * 2, 1000);
    }
    return 'timed_out';
  }

  /** Resolve an approval programmatically (for callbacks, etc.). */
  async resolveApproval(
    approvalId: string,
    resolution: 'allow' | 'deny',
    reason?: string,
  ): Promise<void> {
    try {
      await this.fetchImpl(
        `${this.host}/v1/approvals/${encodeURIComponent(approvalId)}/resolve`,
        {
          method: 'POST',
          headers: this._headers(),
          body: JSON.stringify({ resolution, reason }),
        },
      );
    } catch {
      // Best-effort. The approval will time out server-side if the
      // POST never lands.
    }
  }

  private _headers(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-Lumin-Project': this.project,
    };
    if (this.apiKey) headers['Authorization'] = `Bearer ${this.apiKey}`;
    return headers;
  }

  private _failResponse(reason: string): DecideResponseBody {
    return {
      decision: this.onError === 'deny' ? 'block' : 'allow',
      reason: `firewall_${reason}`,
      policy_name:
        this.onError === 'deny' ? '_firewall_fail_closed' : undefined,
    };
  }

  private _sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms));
  }
}


// ---- admin sender separation -------------------------------------------

export interface AdminSenderRules {
  isAdmin(senderId: string | null | undefined): boolean;
  /** Build the message the LLM should see / produce when a non-admin's
   * call was blocked. Operators may want a different phrasing per
   * deployment; defaults to the generic "contact your administrator". */
  blockedMessageFor(
    senderId: string | null | undefined,
    decision: DecideResponseBody,
    cfg: FirewallConfig,
  ): string;
}

/** Build an admin-rules object from a config. Trim + lowercase so
 * "Telegram:5706" matches "telegram:5706 ". */
export function buildAdminRules(cfg: FirewallConfig): AdminSenderRules {
  const adminSet = new Set(
    (cfg.adminSenders ?? [])
      .map((s) => s.toLowerCase().trim())
      .filter(Boolean),
  );
  const userBlocked = cfg.userBlockedMessage ?? DEFAULT_USER_BLOCKED_MESSAGE;
  const adminSeesFull = cfg.adminSeesFullResponse !== false;

  return {
    isAdmin(senderId) {
      if (!senderId) return false;
      return adminSet.has(senderId.toLowerCase().trim());
    },
    blockedMessageFor(senderId, decision, _innerCfg) {
      const isAdmin = !!senderId && adminSet.has(senderId.toLowerCase().trim());
      if (isAdmin && adminSeesFull) {
        // Surface as much detail as the LLM can use — this is the
        // operator's debug surface.
        const parts: string[] = [];
        if (decision.policy_name) parts.push(`policy=${decision.policy_name}`);
        if (decision.reason) parts.push(decision.reason);
        if (decision.agent_feedback) parts.push(decision.agent_feedback);
        return parts.length > 0
          ? `Lumin firewall: ${parts.join(' — ')}`
          : userBlocked;
      }
      return userBlocked;
    },
  };
}


// ---- decision → tool-result translation -------------------------------

export interface FirewallToolResult {
  /** True when the firewall denied the call. The Mastra tool wrapper
   * uses this to throw / return an error result instead of running the
   * underlying tool. */
  blocked: boolean;
  /** Operator-facing reason. Always set when blocked=true. */
  reason?: string;
  /** Message the wrapper should surface to the LLM. Already
   * sender-aware (admin-vs-non-admin) and includes ``agent_feedback``
   * when applicable. */
  llmFeedback?: string;
  /** Rewrite payload — when the decision is rewrite, the wrapper
   * substitutes these params before calling the tool. */
  rewrittenParams?: Record<string, unknown>;
  /** Server's decision verb — useful for logging / observability. */
  decision: DecisionVerb;
  /** Forwarded straight from the decide response so the wrapper can
   * record which Lumin decision this corresponds to. */
  decisionId?: string;
  approvalId?: string;
}


/** Translate a /v1/policy/decide response into a wrapper-friendly
 * shape. ``allow`` and ``flag`` produce ``blocked: false``; the
 * other verbs map according to §5.4. ``require_approval`` is
 * resolved by the caller via ``LuminFirewallClient.waitForApproval``
 * — this function returns the raw approval_id so the caller can
 * choose a sync or async approval flow. */
export function translateDecision(
  decision: DecideResponseBody,
  rules: AdminSenderRules,
  cfg: FirewallConfig,
  senderId?: string,
): FirewallToolResult {
  const baseFeedback = rules.blockedMessageFor(senderId, decision, cfg);

  if (!decision || decision.decision === 'allow' || decision.decision === 'flag') {
    return { blocked: false, decision: decision?.decision ?? 'allow' };
  }

  if (decision.decision === 'block') {
    return {
      blocked: true,
      reason: decision.reason || 'blocked by Lumin firewall',
      llmFeedback: baseFeedback,
      decision: 'block',
      decisionId: decision.decision_id,
    };
  }

  if (decision.decision === 'rewrite') {
    const params = decision.rewritten?.params;
    if (params && typeof params === 'object') {
      return {
        blocked: false,
        rewrittenParams: params,
        decision: 'rewrite',
        decisionId: decision.decision_id,
      };
    }
    // Server returned rewrite without params payload — degrade to
    // block rather than silently letting the original through.
    return {
      blocked: true,
      reason: decision.reason || 'rewrite without payload',
      llmFeedback: baseFeedback,
      decision: 'block',
      decisionId: decision.decision_id,
    };
  }

  if (decision.decision === 'require_approval') {
    return {
      blocked: true,  // wrapper holds the call until approval resolves
      reason: decision.reason || 'awaiting operator approval',
      llmFeedback: baseFeedback,
      decision: 'require_approval',
      decisionId: decision.decision_id,
      approvalId: decision.approval_id,
    };
  }

  // Unknown decision verb — Rule 7. Allow the call but flag in logs.
  // eslint-disable-next-line no-console
  console.warn(
    `[lumin-mastra] unknown decision verb ${String(decision.decision)}; allowing`,
  );
  return { blocked: false, decision: 'allow' };
}
