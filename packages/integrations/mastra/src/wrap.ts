/**
 * @lumin-io/mastra — tool wrapper for synchronous firewall enforcement.
 *
 * Operators wrap any Mastra Tool with ``wrapToolWithFirewall(tool, cfg)``;
 * the returned tool runs the same logic but routes every invocation
 * through Lumin's /v1/policy/decide endpoint first.
 *
 * Why a wrapper rather than a Mastra plugin: Mastra's plugin API is
 * still evolving, and tool-level wrapping works on every Mastra
 * version that exposes the standard ``execute`` shape. Operators
 * who later move to a plugin-based model can swap the wrapper for
 * the plugin without changing call sites.
 *
 * Mastra Tool shape (compatible across recent versions):
 *
 *   interface Tool {
 *     id: string;
 *     description?: string;
 *     execute: (ctx: { context: Record<string, unknown>, ... }) => Promise<unknown>;
 *     // other fields preserved as-is
 *   }
 *
 * The wrapper is structural — anything with ``id`` + ``execute`` works,
 * including Tools built via ``createTool({...})``.
 */

import {
  buildAdminRules,
  LuminFirewallClient,
  translateDecision,
  type DecideResponseBody,
  type FirewallConfig,
  type FirewallToolResult,
} from './firewall.js';


/** Minimal structural type the wrapper needs from a Mastra Tool.
 * Any Tool that has these two properties + an ``execute`` method
 * works — including the canonical createTool({...}) output. */
export interface MastraToolLike {
  id: string;
  description?: string;
  execute: (ctx: ToolExecutionContext) => Promise<unknown>;
  [extra: string]: unknown;
}

export interface ToolExecutionContext {
  context: Record<string, unknown>;
  /** Mastra runtime context — we read trace_id / agent / sender_id
   * from here when present, but tolerate missing keys. */
  runtimeContext?: {
    get?: (key: string) => unknown;
  } | Record<string, unknown>;
  /** Some Mastra versions pass tracing context directly; we sniff
   * both shapes in extractDecideContext below. */
  tracingContext?: unknown;
  [extra: string]: unknown;
}

export interface WrapOptions extends FirewallConfig {
  /** Pre-built client. If omitted, a fresh ``LuminFirewallClient`` is
   * constructed from the rest of the config — sharing one client
   * across many tool wrappers is the common case. */
  client?: LuminFirewallClient;
  /** The agent name to record on every decide call. Used by the
   * dashboard's per-agent timeline filter. Mastra sets the agent
   * from the surrounding agent definition; we accept it explicitly
   * because the tool itself doesn't always know its agent. */
  agent?: string;
  /** When set, called after each decide to let operators stash the
   * decision_id alongside their own telemetry. Errors thrown by
   * this callback are swallowed (Rule 7). */
  onDecision?: (
    body: DecideResponseBody,
    result: FirewallToolResult,
  ) => void;
}


/** Wrap a Mastra tool so every invocation passes through Lumin's
 * synchronous decide endpoint first. The returned tool has the
 * same id + description; only ``execute`` is replaced.
 *
 * Behavior matrix:
 *
 *   allow / flag        → run the tool, return its result
 *   block               → throw Error(llmFeedback)
 *   rewrite             → run the tool with rewritten params
 *   require_approval    → long-poll the approval; if allowed, run
 *                         the tool; otherwise throw with feedback
 *
 * Failures of the firewall itself follow ``onFirewallError`` — by
 * default, allow (Rule 7). This matches the OpenClaw plugin's
 * v0.4.0 default behavior exactly.
 */
export function wrapToolWithFirewall<T extends MastraToolLike>(
  tool: T,
  opts: WrapOptions = {},
): T {
  const client = opts.client ?? new LuminFirewallClient(opts);
  const adminRules = buildAdminRules(opts);
  const originalExecute = tool.execute.bind(tool);

  const wrappedExecute: T['execute'] = async (ctx) => {
    const decideCtx = extractDecideContext(ctx, opts);
    const decision = await client.decide({
      lifecycle: 'before_tool_call',
      tool_name: tool.id,
      params: ctx.context,
      ...decideCtx,
    });

    let result = translateDecision(
      decision,
      adminRules,
      opts,
      decideCtx.sender_id,
    );

    // Approval flow — caller can opt out by setting approvalTimeoutMs
    // to 0, in which case we treat require_approval as a hard block.
    if (
      result.decision === 'require_approval' &&
      result.approvalId &&
      (opts.approvalTimeoutMs ?? 1) > 0
    ) {
      const verdict = await client.waitForApproval(
        result.approvalId,
        opts.approvalTimeoutMs,
      );
      if (verdict === 'allowed') {
        // Re-fetch is unnecessary — we got the green light.
        result = {
          ...result,
          blocked: false,
          decision: 'allow',
        };
      } else {
        // denied or timed_out — keep the block, but make the reason
        // more specific for operator logs.
        result = {
          ...result,
          reason:
            verdict === 'timed_out'
              ? 'approval_timed_out'
              : 'approval_denied',
        };
      }
    }

    try {
      opts.onDecision?.(decision, result);
    } catch {
      // Rule 7
    }

    if (result.blocked) {
      const message =
        result.llmFeedback || result.reason || 'Lumin firewall blocked';
      // Throwing rather than returning an error structure: Mastra
      // catches tool exceptions and surfaces them as tool-call errors
      // to the LLM, which is exactly the loop we want to break out of
      // a misbehaving tool sequence with feedback.
      const err = new FirewallBlockedError(message, result);
      throw err;
    }

    // Rewrite path — substitute params before calling the underlying
    // tool. We mutate the context in-place because creating a fresh
    // ToolExecutionContext loses Mastra runtime fields we don't know
    // about.
    if (result.rewrittenParams) {
      ctx.context = result.rewrittenParams;
    }

    return originalExecute(ctx);
  };

  return { ...tool, execute: wrappedExecute };
}


/** Thrown when the firewall blocks (or denies an approval for) a
 * tool call. The Mastra runtime catches it and surfaces the message
 * to the LLM. The original ``FirewallToolResult`` is preserved so
 * upstream code can introspect (decision verb, approval_id, etc.)
 * if needed. */
export class FirewallBlockedError extends Error {
  readonly result: FirewallToolResult;

  constructor(message: string, result: FirewallToolResult) {
    super(message);
    this.name = 'FirewallBlockedError';
    this.result = result;
  }
}


/** Pull whatever decide-context fields we can find from Mastra's
 * runtime context. Mastra evolves quickly — different versions stash
 * trace info under different keys — so we sniff a small set of
 * common shapes. Missing fields are fine; the API treats them as
 * optional. */
function extractDecideContext(
  ctx: ToolExecutionContext,
  opts: WrapOptions,
): {
  trace_id?: string;
  span_id?: string;
  session_id?: string;
  agent?: string;
  project?: string;
  sender_id?: string;
} {
  const runtime = ctx.runtimeContext;
  const get = (key: string): string | undefined => {
    if (!runtime) return undefined;
    if (typeof (runtime as { get?: unknown }).get === 'function') {
      const v = (runtime as { get: (k: string) => unknown }).get(key);
      return typeof v === 'string' ? v : undefined;
    }
    const v = (runtime as Record<string, unknown>)[key];
    return typeof v === 'string' ? v : undefined;
  };

  return {
    trace_id: get('trace_id') || get('traceId'),
    span_id: get('span_id') || get('spanId'),
    session_id: get('session_id') || get('sessionId') || get('threadId'),
    agent: opts.agent ?? (get('agent') || get('agentName')),
    project: opts.project,
    sender_id: get('sender_id') || get('senderId') || get('userId'),
  };
}
