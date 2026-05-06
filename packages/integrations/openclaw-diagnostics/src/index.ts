/**
 * @lumin-io/openclaw-diagnostics — Lumin's deepest hook into OpenClaw.
 *
 * Two related but separate APIs in OpenClaw observe a model call:
 *
 *   1. `internalDiagnostics.onEvent` — the bus the bundled
 *      `@openclaw/diagnostics-otel` plugin reads. Carries timing,
 *      sizes, and provider IDs but the runtime never populates the
 *      `inputMessages` / `outputMessages` fields the OTel exporter
 *      tries to read, so the diagnostics-otel content-capture flag
 *      is effectively non-functional in 2026.5.x. Fixing that needs
 *      an upstream PR; meanwhile we route around it.
 *
 *   2. **Typed hooks** (`llm_input`, `llm_output`) — the registration
 *      surface used by trusted plugins. These DO carry full content
 *      (`prompt`, `historyMessages`, `systemPrompt`, `assistantTexts`,
 *      `usage`) at runtime, which is exactly what an observability
 *      tool needs.
 *
 * This plugin uses (2). On every llm_input / llm_output we build a
 * Lumin SpanInput and POST to `/v1/spans`. The agent never blocks on
 * us — failures are swallowed and budgeted with a short timeout.
 *
 * Activation prerequisite: non-bundled plugins must opt into
 * conversation access, so the operator's openclaw.json must contain
 *
 *   "plugins": {
 *     "entries": {
 *       "lumin-diagnostics": {
 *         "hooks": { "allowConversationAccess": true }
 *       }
 *     }
 *   }
 *
 * The plugin's install step writes that for the operator; if it's
 * missing, OpenClaw silently drops the hook registration and we
 * never see content. The plugin warns once at startup if the flag
 * isn't set so the misconfiguration surfaces immediately.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// ----- public config shape ------------------------------------------------

interface LuminDiagnosticsConfig {
  host?: string;
  project?: string;
  captureSystemPrompt?: boolean;
  maxContentChars?: number;
  timeoutMs?: number;
}

const DEFAULT_HOST = "http://localhost:8000";
const DEFAULT_PROJECT = "openclaw";
const DEFAULT_MAX_CONTENT_CHARS = 32_768;
const DEFAULT_TIMEOUT_MS = 5_000;


// ----- runtime hook event shapes -----------------------------------------
//
// Mirrors `PluginHookLlmInputEvent` / `PluginHookLlmOutputEvent` from
// `openclaw/plugin-sdk/src/plugins/hook-types.d.ts`. We re-declare here
// rather than importing because the public type re-export surface is
// in flux; the field set we read is small and stable.

interface LlmInputEvent {
  runId: string;
  sessionId: string;
  provider: string;
  model: string;
  systemPrompt?: string;
  prompt: string;
  historyMessages: unknown[];
  imagesCount?: number;
}

interface LlmOutputEvent {
  runId: string;
  sessionId: string;
  provider: string;
  model: string;
  resolvedRef?: string;
  harnessId?: string;
  assistantTexts: string[];
  lastAssistant?: unknown;
  usage?: {
    input?: number;
    output?: number;
    cacheRead?: number;
    cacheWrite?: number;
    total?: number;
  };
}

interface BeforeToolCallEvent {
  toolName: string;
  params: Record<string, unknown>;
  runId?: string;
  toolCallId?: string;
}

interface AfterToolCallEvent {
  toolName: string;
  params: Record<string, unknown>;
  runId?: string;
  toolCallId?: string;
  result?: unknown;
  error?: string;
  durationMs?: number;
}

interface HookContext {
  runId?: string;
  jobId?: string;
  trace?: { traceId?: string; spanId?: string; parentSpanId?: string; traceFlags?: string };
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  workspaceDir?: string;
  modelProviderId?: string;
  modelId?: string;
  trigger?: string;
  channelId?: string;
}


// ----- helpers ------------------------------------------------------------

function stringify(value: unknown, maxLen: number): string | undefined {
  if (value === null || value === undefined) return undefined;
  let s: string;
  if (typeof value === "string") {
    s = value;
  } else if (Array.isArray(value)) {
    s = value
      .map((v) => (typeof v === "string" ? v : safeJsonStringify(v)))
      .join("\n");
  } else {
    s = safeJsonStringify(value);
  }
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen - 16) + "…(truncated)";
}

function safeJsonStringify(v: unknown): string {
  try {
    const out = JSON.stringify(v);
    return out === undefined ? String(v) : out;
  } catch {
    return String(v);
  }
}

/**
 * Format an OpenClaw 32-hex traceId / 16-hex spanId as a UUID so it
 * collides with the trace_id schema Lumin already uses for OTLP-
 * ingested spans. Also pads / hashes a fallback so we always get a
 * deterministic 32-hex ID even when the hook runs without a trace
 * scope.
 */
function asUuid(hex: string | undefined, fallback: string): string {
  const h = (hex || "").toLowerCase().replace(/[^0-9a-f]/g, "");
  const padded = h.length >= 32 ? h.slice(0, 32) : h.padStart(32, "0");
  if (!padded || /^0+$/.test(padded)) {
    const fp = fnv1a64(fallback).padStart(32, "0").slice(-32);
    return uuidify(fp);
  }
  return uuidify(padded);
}

function uuidify(hex32: string): string {
  return `${hex32.slice(0, 8)}-${hex32.slice(8, 12)}-${hex32.slice(12, 16)}-${hex32.slice(16, 20)}-${hex32.slice(20, 32)}`;
}

function fnv1a64(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  let h2 = 0xcbf29ce4;
  for (let i = s.length - 1; i >= 0; i--) {
    h2 ^= s.charCodeAt(i);
    h2 = Math.imul(h2, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0");
}

function nowIso(): string {
  return new Date().toISOString().replace("Z", "000Z");
}


/**
 * Walk an OpenClaw assistant message's content blocks and concatenate
 * any ``{type: "thinking", thinking: "..."}`` payloads. Reasoning
 * models attach these BEFORE the visible text, but
 * ``assistantTexts`` strips them. Returning undefined means "no
 * thinking blocks present" — the caller should leave the metadata
 * field absent rather than write an empty string.
 *
 * Defensive: lastAssistant is typed as ``unknown`` and provider
 * shapes drift, so every step type-checks before recursing.
 */
function extractThinkingFromAssistant(lastAssistant: unknown): string | undefined {
  if (!lastAssistant || typeof lastAssistant !== "object") return undefined;
  const content = (lastAssistant as { content?: unknown }).content;
  if (!Array.isArray(content)) return undefined;
  const parts: string[] = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    const b = block as { type?: unknown; thinking?: unknown; text?: unknown };
    if (b.type === "thinking" && typeof b.thinking === "string" && b.thinking.length > 0) {
      parts.push(b.thinking);
    }
  }
  return parts.length > 0 ? parts.join("\n\n") : undefined;
}


// ----- transport ----------------------------------------------------------

class LuminClient {
  private host: string;
  private project: string;
  private timeoutMs: number;
  private failureLogged = false;

  constructor(cfg: LuminDiagnosticsConfig) {
    // Host is sourced from the operator's openclaw.json config only.
    // For Docker Compose / Kubernetes deployments, set
    // ``plugins.entries.lumin-diagnostics.config.host`` in the
    // mounted config file — that's the standard pattern.
    this.host = (cfg.host || DEFAULT_HOST).replace(/\/+$/, "");
    this.project = cfg.project || DEFAULT_PROJECT;
    this.timeoutMs = cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  /**
   * Fire-and-forget POST. Rule 7 generalized: a Lumin outage must
   * never affect the agent. The first failure logs once; subsequent
   * failures are silenced.
   */
  async send(span: Record<string, unknown>): Promise<void> {
    const body = JSON.stringify({ spans: [span] });
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const resp = await fetch(`${this.host}/v1/spans`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Lumin-Project": this.project,
        },
        body,
        signal: ctrl.signal,
      });
      if (!resp.ok && !this.failureLogged) {
        // eslint-disable-next-line no-console
        console.warn(
          `[lumin-diagnostics] Lumin returned ${resp.status} ${resp.statusText} — further failures suppressed`,
        );
        this.failureLogged = true;
      }
    } catch (err) {
      if (!this.failureLogged) {
        // eslint-disable-next-line no-console
        console.warn(
          `[lumin-diagnostics] Could not reach Lumin at ${this.host}/v1/spans (${(err as Error).message}). Further failures suppressed.`,
        );
        this.failureLogged = true;
      }
    } finally {
      clearTimeout(timer);
    }
  }
}


// ----- pending llm_input registry ----------------------------------------
//
// llm_input fires before the model call, llm_output fires after. We
// stitch them via runId so a single Lumin span carries both halves.
// If a run errors before output, the in-flight entry is dropped after
// a hard cap (default 5 min) so a stalled hook can't leak memory.

interface PendingLlmCall {
  startedAt: string;
  startedAtMs: number;
  systemPrompt?: string;
  systemPromptChars?: number;
  historyMessageCount?: number;
  input?: string;
  imagesCount?: number;
  trace?: HookContext["trace"];
}

const PENDING_TTL_MS = 5 * 60 * 1000;

class PendingLlmRegistry {
  private byRunId = new Map<string, PendingLlmCall>();
  private cleanupHandle: ReturnType<typeof setTimeout> | undefined;

  set(runId: string, entry: PendingLlmCall): void {
    this.byRunId.set(runId, entry);
    this.scheduleSweep();
  }

  take(runId: string): PendingLlmCall | undefined {
    const v = this.byRunId.get(runId);
    if (v) this.byRunId.delete(runId);
    return v;
  }

  size(): number {
    return this.byRunId.size;
  }

  private scheduleSweep(): void {
    if (this.cleanupHandle) return;
    this.cleanupHandle = setTimeout(() => {
      this.cleanupHandle = undefined;
      const now = Date.now();
      for (const [k, v] of this.byRunId) {
        if (now - v.startedAtMs > PENDING_TTL_MS) this.byRunId.delete(k);
      }
      if (this.byRunId.size > 0) this.scheduleSweep();
    }, PENDING_TTL_MS);
    // unref so a sweeper doesn't keep the gateway process alive
    if (typeof this.cleanupHandle === "object" && this.cleanupHandle && "unref" in this.cleanupHandle) {
      (this.cleanupHandle as { unref?: () => void }).unref?.();
    }
  }
}


// ----- pending tool-call registry ---------------------------------------
//
// Tools have their own lifecycle: ``before_tool_call`` carries the
// invocation params; ``after_tool_call`` carries the result + duration.
// The pair is correlated by ``toolCallId`` (stable across the two
// events when the host populates it; we fall back to a synthetic key
// derived from ``runId + toolName + ts`` when it's missing — only
// matters if a single run somehow fires before/after for two tools
// with no toolCallId, which the upstream API doesn't actually do).

interface PendingToolCall {
  startedAt: string;
  startedAtMs: number;
  toolName: string;
  params?: Record<string, unknown>;
  trace?: HookContext["trace"];
  runId?: string;
}

class PendingToolCallRegistry {
  private byKey = new Map<string, PendingToolCall>();
  private cleanupHandle: ReturnType<typeof setTimeout> | undefined;

  set(key: string, entry: PendingToolCall): void {
    this.byKey.set(key, entry);
    this.scheduleSweep();
  }

  take(key: string): PendingToolCall | undefined {
    const v = this.byKey.get(key);
    if (v) this.byKey.delete(key);
    return v;
  }

  size(): number {
    return this.byKey.size;
  }

  private scheduleSweep(): void {
    if (this.cleanupHandle) return;
    this.cleanupHandle = setTimeout(() => {
      this.cleanupHandle = undefined;
      const now = Date.now();
      for (const [k, v] of this.byKey) {
        if (now - v.startedAtMs > PENDING_TTL_MS) this.byKey.delete(k);
      }
      if (this.byKey.size > 0) this.scheduleSweep();
    }, PENDING_TTL_MS);
    if (typeof this.cleanupHandle === "object" && this.cleanupHandle && "unref" in this.cleanupHandle) {
      (this.cleanupHandle as { unref?: () => void }).unref?.();
    }
  }
}


function toolCallKey(runId: string | undefined, toolCallId: string | undefined, toolName: string): string {
  // Prefer toolCallId — it's the host's canonical identifier and
  // doesn't collide across concurrent same-tool calls within a run.
  // When missing, the run+tool fallback is good-enough since OpenClaw
  // serializes tool calls per agent turn (one before/after pair
  // outstanding at a time per run).
  if (toolCallId) return `tcid:${toolCallId}`;
  return `rt:${runId ?? "_"}::${toolName}`;
}


// ----- per-event translators ---------------------------------------------

function buildSpanFromPair(
  runId: string,
  pending: PendingLlmCall,
  output: LlmOutputEvent,
  cfg: LuminDiagnosticsConfig,
  hookCtx: HookContext | undefined,
): Record<string, unknown> {
  const maxLen = cfg.maxContentChars ?? DEFAULT_MAX_CONTENT_CHARS;
  const trace = hookCtx?.trace || pending.trace;
  const traceId = asUuid(trace?.traceId, runId);
  const spanId = asUuid(trace?.spanId, `${runId}:llm`);
  const parentId = trace?.parentSpanId
    ? asUuid(trace.parentSpanId, runId)
    : undefined;

  const usage = output.usage || {};
  const outputText = output.assistantTexts && output.assistantTexts.length
    ? output.assistantTexts.join("\n")
    : (output.lastAssistant !== undefined ? safeJsonStringify(output.lastAssistant) : undefined);

  // Reasoning models (gpt-oss, claude-extended-thinking, o-series) emit
  // ``{type: "thinking", thinking: "..."}`` blocks under
  // ``lastAssistant.content`` BEFORE the visible text. ``assistantTexts``
  // strips those out — fine for the operator-facing output, but we
  // lose the reasoning trace entirely. Pull thinking out separately
  // and surface it as a metadata field so operators can see WHY the
  // model answered the way it did. Defensive parse: lastAssistant is
  // typed as unknown by the public hook contract, so we check before
  // walking it.
  const thinkingText = extractThinkingFromAssistant(output.lastAssistant);

  return {
    id: spanId,
    trace_id: traceId,
    parent_span_id: parentId,
    name: "openclaw.llm",
    type: "llm",
    started_at: pending.startedAt,
    ended_at: nowIso(),
    status: "ok",
    model: output.model,
    provider: output.provider,
    tokens_input: usage.input,
    tokens_output: usage.output,
    input: pending.input,
    output: outputText ? stringify(outputText, maxLen) : undefined,
    session_id: output.sessionId || hookCtx?.sessionId,
    metadata: {
      "openclaw.runId": runId,
      "openclaw.harnessId": output.harnessId,
      "openclaw.resolvedRef": output.resolvedRef,
      "openclaw.images_count": pending.imagesCount,
      // Lightweight summary of what was replayed to the model, so an
      // operator can see "this turn carried N prior messages and an
      // M-character system-role message" without dragging the actual
      // payload into the trace's input field.
      "openclaw.history_message_count": pending.historyMessageCount,
      "openclaw.system_prompt_chars": pending.systemPromptChars,
      // Reasoning trace for models that emit thinking blocks. Always
      // captured (no opt-in) because the whole point of an
      // observability tool is to show WHY the agent answered the way
      // it did — silently dropping the reasoning would defeat the
      // purpose. Field is absent (not empty) when the model didn't
      // emit thinking, so dashboard rendering can branch on
      // presence rather than length.
      ...(thinkingText !== undefined
        ? {
            "openclaw.content.thinking": stringify(thinkingText, maxLen),
            "openclaw.thinking_chars": thinkingText.length,
          }
        : {}),
      ...(cfg.captureSystemPrompt && pending.systemPrompt
        ? { "openclaw.content.system_prompt": stringify(pending.systemPrompt, maxLen) }
        : {}),
    },
  };
}


function buildSpanFromToolCall(
  before: PendingToolCall,
  after: AfterToolCallEvent,
  cfg: LuminDiagnosticsConfig,
  hookCtx: HookContext | undefined,
): Record<string, unknown> {
  const maxLen = cfg.maxContentChars ?? DEFAULT_MAX_CONTENT_CHARS;
  const trace = hookCtx?.trace || before.trace;
  const runId = after.runId ?? before.runId ?? "_";
  // Tool spans share the run's traceId with the LLM span — that's
  // exactly what fuses them into one trace timeline on the dashboard.
  const traceId = asUuid(trace?.traceId, runId);
  // Each tool call gets its own deterministic spanId derived from
  // toolCallId + toolName so re-ingest of the same call lands on
  // the same span row (idempotent like the LLM path).
  const fp = `${runId}:tool:${after.toolCallId ?? after.toolName}`;
  const spanId = asUuid(undefined, fp);
  // Parent: the run's root span (so the tool call nests under the
  // openclaw run in the timeline). We DON'T use trace.spanId as the
  // parent because that's our own llm-call's spanId in the registry
  // — we want the run-level parent. Falls back to undefined if the
  // hook context didn't expose one; the dashboard still renders the
  // tool span as a top-level entry under the openclaw trace.
  const parentId = trace?.parentSpanId
    ? asUuid(trace.parentSpanId, runId)
    : undefined;

  const isError = typeof after.error === "string" && after.error.length > 0;

  return {
    id: spanId,
    trace_id: traceId,
    parent_span_id: parentId,
    name: "openclaw.tool.call",
    type: "tool",
    started_at: before.startedAt,
    ended_at: nowIso(),
    status: isError ? "error" : "ok",
    error_message: isError ? after.error : undefined,
    tool_name: after.toolName,
    input: stringify(before.params ?? after.params ?? {}, maxLen),
    output: after.result !== undefined ? stringify(after.result, maxLen) : undefined,
    session_id: hookCtx?.sessionId,
    duration_ms: after.durationMs,
    metadata: {
      "openclaw.runId": runId,
      "openclaw.toolCallId": after.toolCallId,
      "openclaw.toolName": after.toolName,
    },
  };
}


// ----- plugin entry -------------------------------------------------------

export default definePluginEntry({
  id: "lumin-diagnostics",
  name: "@lumin-io/openclaw-diagnostics",
  description:
    "Streams full-fidelity OpenClaw runs (prompts, responses, tool I/O, tokens) to a local Lumin instance via /v1/spans.",
  configSchema: {
    type: "object",
    properties: {
      host: { type: "string" },
      project: { type: "string" },
      captureSystemPrompt: { type: "boolean" },
      maxContentChars: { type: "number" },
      timeoutMs: { type: "number" },
    },
  } as never,
  register: (api): void => {
    const apiAny = api as unknown as {
      pluginConfig?: LuminDiagnosticsConfig;
      logger?: { info?: (s: string) => void; warn?: (s: string) => void };
      on?: (
        hookName: string,
        handler: (event: unknown, ctx: unknown) => unknown | Promise<unknown>,
        opts?: { priority?: number; timeoutMs?: number },
      ) => void;
    };
    const cfg: LuminDiagnosticsConfig = apiAny.pluginConfig || {};
    const client = new LuminClient(cfg);
    const pending = new PendingLlmRegistry();
    const toolPending = new PendingToolCallRegistry();
    const log = apiAny.logger;

    if (typeof apiAny.on !== "function") {
      // Older OpenClaw runtimes without typed-hook support won't
      // expose `api.on`. Silently degrade: register nothing.
      log?.warn?.(
        "lumin-diagnostics: this OpenClaw build doesn't expose api.on for typed hooks; content capture disabled. Upgrade OpenClaw to >= 2026.5.x.",
      );
      return;
    }

    apiAny.on("llm_input", (rawEvent: unknown, rawCtx: unknown) => {
      try {
        const event = rawEvent as LlmInputEvent;
        const ctx = rawCtx as HookContext | undefined;
        const maxLen = cfg.maxContentChars ?? DEFAULT_MAX_CONTENT_CHARS;
        // The input is JUST the user prompt for this turn. We
        // deliberately do NOT pack history / systemPrompt into the
        // input field: a Lumin trace represents one LLM call, not
        // a conversation. Embedding the full chat history every
        // turn (a) bloats every trace by 10x+ as conversations
        // grow, (b) makes the dashboard view feel like a chat log
        // instead of an agent run, and (c) is redundant with
        // sessions, which group turns under the same conversation
        // already.
        //
        // Counts and lightweight summaries go into metadata so
        // operators can still see "this turn replayed 9 prior
        // history messages" without the full payload.
        const historyCount = Array.isArray(event.historyMessages)
          ? event.historyMessages.length
          : 0;
        pending.set(event.runId, {
          startedAt: nowIso(),
          startedAtMs: Date.now(),
          systemPrompt: event.systemPrompt,
          systemPromptChars: typeof event.systemPrompt === "string"
            ? event.systemPrompt.length
            : 0,
          historyMessageCount: historyCount,
          input: stringify(event.prompt, maxLen),
          imagesCount: event.imagesCount,
          trace: ctx?.trace,
        });
      } catch (err) {
        log?.warn?.(`lumin-diagnostics: llm_input handler failed: ${(err as Error).message}`);
      }
    });

    apiAny.on("llm_output", (rawEvent: unknown, rawCtx: unknown) => {
      try {
        const event = rawEvent as LlmOutputEvent;
        const ctx = rawCtx as HookContext | undefined;
        const entry = pending.take(event.runId);
        if (!entry) {
          // No matching llm_input. Either the input hook dropped
          // (e.g. raw model run path) or this is a fresh restart
          // catching the tail of an in-flight run. Emit anyway —
          // the operator still gets the assistant output.
          const fallback: PendingLlmCall = {
            startedAt: nowIso(),
            startedAtMs: Date.now(),
            trace: ctx?.trace,
          };
          const span = buildSpanFromPair(event.runId, fallback, event, cfg, ctx);
          void client.send(span).catch(() => {});
          return;
        }
        const span = buildSpanFromPair(event.runId, entry, event, cfg, ctx);
        void client.send(span).catch(() => {});
      } catch (err) {
        log?.warn?.(`lumin-diagnostics: llm_output handler failed: ${(err as Error).message}`);
      }
    });

    // Tool hooks. Pair before/after via toolCallId (or runId+toolName
    // fallback when the host doesn't populate it). The before-hook
    // captures the params + start time; the after-hook attaches the
    // result + duration and ships the span. ``before_tool_call`` and
    // ``after_tool_call`` aren't conversation-gated upstream, so they
    // register without any extra config beyond what llm_input already
    // required for this plugin.
    apiAny.on("before_tool_call", (rawEvent: unknown, rawCtx: unknown) => {
      try {
        const event = rawEvent as BeforeToolCallEvent;
        const ctx = rawCtx as HookContext | undefined;
        toolPending.set(toolCallKey(event.runId, event.toolCallId, event.toolName), {
          startedAt: nowIso(),
          startedAtMs: Date.now(),
          toolName: event.toolName,
          params: event.params,
          trace: ctx?.trace,
          runId: event.runId,
        });
      } catch (err) {
        log?.warn?.(`lumin-diagnostics: before_tool_call handler failed: ${(err as Error).message}`);
      }
    });

    apiAny.on("after_tool_call", (rawEvent: unknown, rawCtx: unknown) => {
      try {
        const event = rawEvent as AfterToolCallEvent;
        const ctx = rawCtx as HookContext | undefined;
        const key = toolCallKey(event.runId, event.toolCallId, event.toolName);
        const entry = toolPending.take(key) ?? {
          // Orphan after-call (e.g. before-hook missed because the
          // plugin loaded mid-run). Synthesize a zero-duration entry
          // so the span still reports the result + tool name.
          startedAt: nowIso(),
          startedAtMs: Date.now(),
          toolName: event.toolName,
          params: event.params,
          trace: ctx?.trace,
          runId: event.runId,
        };
        const span = buildSpanFromToolCall(entry, event, cfg, ctx);
        void client.send(span).catch(() => {});
      } catch (err) {
        log?.warn?.(`lumin-diagnostics: after_tool_call handler failed: ${(err as Error).message}`);
      }
    });

    log?.info?.(
      `lumin-diagnostics: subscribed to llm_input + llm_output + before_tool_call + after_tool_call → ${cfg.host || DEFAULT_HOST}/v1/spans (project=${cfg.project || DEFAULT_PROJECT})`,
    );
  },
});
