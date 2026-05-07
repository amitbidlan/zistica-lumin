/**
 * @lumin-io/mastra — local-first observability + Agent Firewall for
 * Mastra agents.
 *
 * Public API:
 *   - LuminExporter: OTel-compatible span exporter that ships to
 *     a running Lumin instance (default http://localhost:8000).
 *   - luminConfig(): helper that returns the full Mastra
 *     `observability` config block, pre-wired with a LuminExporter.
 *   - installLuminTracing(): attaches a LuminExporter to the
 *     active OTel tracer provider when LUMIN_TRACING=true. Most
 *     users will use the `import "@lumin-io/mastra/auto"` form.
 *   - wrapToolWithFirewall(): higher-order wrapper that adds
 *     synchronous /v1/policy/decide enforcement to any Mastra
 *     tool. Mirrors the OpenClaw plugin v0.4.0 firewall behavior
 *     — block / rewrite / require_approval / allow.
 *   - LuminFirewallClient: low-level decide + waitForApproval
 *     client for operators building their own integration.
 */

export {
  LuminExporter,
  otelSpanToLumin,
  registerModelPrice,
} from './exporter.js';
export type { LuminExporterConfig } from './exporter.js';

export { luminConfig } from './config.js';
export type { LuminConfigOptions } from './config.js';

export { installLuminTracing, tryAttach } from './auto.js';

// ----- Agent Firewall (Slice 3 PR J) -----
export {
  LuminFirewallClient,
  buildAdminRules,
  translateDecision,
} from './firewall.js';
export type {
  FirewallConfig,
  DecideRequestBody,
  DecideResponseBody,
  DecisionVerb,
  DecideLifecycle,
  AdminSenderRules,
  FirewallToolResult,
} from './firewall.js';

export {
  wrapToolWithFirewall,
  FirewallBlockedError,
} from './wrap.js';
export type {
  MastraToolLike,
  ToolExecutionContext,
  WrapOptions,
} from './wrap.js';
