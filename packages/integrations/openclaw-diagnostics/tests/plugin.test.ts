/**
 * Unit tests for @lumin-io/openclaw-diagnostics.
 *
 * Strategy: stub `openclaw/plugin-sdk/plugin-entry` so `definePluginEntry`
 * just returns the options object verbatim. That gives us direct access
 * to `register(api)` without needing the real OpenClaw runtime. Inside
 * each test we hand-build a minimal `api` shape (just `.on`, `.logger`,
 * and `.pluginConfig`) and a stubbed `fetch` so we can capture the
 * spans the plugin would have POSTed to Lumin.
 *
 * The tests focus on the wire contract — what the plugin sends to Lumin
 * — not implementation internals. Refactors that keep the contract
 * stable should leave these green.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("openclaw/plugin-sdk/plugin-entry", () => ({
  // The real definePluginEntry validates + freezes; ours just forwards
  // so register() and the rest stay accessible to the test.
  definePluginEntry: (opts: unknown) => opts,
}));

// Import AFTER the mock so the module graph picks up our stub.
// Re-import inside each test (resetModules below) so module-level
// state (e.g., the failure-logged latch in LuminClient) doesn't
// leak across cases.
import luminPlugin from "../src/index";

interface RegisteredHooks {
  llm_input?: (event: unknown, ctx: unknown) => unknown;
  llm_output?: (event: unknown, ctx: unknown) => unknown;
}

interface FakeApi {
  pluginConfig?: Record<string, unknown>;
  logger: { info: ReturnType<typeof vi.fn>; warn: ReturnType<typeof vi.fn> };
  on: (name: string, handler: (e: unknown, c: unknown) => unknown) => void;
}

function buildFakeApi(pluginConfig?: Record<string, unknown>): {
  api: FakeApi;
  hooks: RegisteredHooks;
} {
  const hooks: RegisteredHooks = {};
  const api: FakeApi = {
    pluginConfig,
    logger: { info: vi.fn(), warn: vi.fn() },
    on: (name, handler) => {
      if (name === "llm_input") hooks.llm_input = handler;
      else if (name === "llm_output") hooks.llm_output = handler;
    },
  };
  return { api, hooks };
}

let fetchMock: ReturnType<typeof vi.fn>;
let originalFetch: typeof fetch | undefined;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "OK",
  } as Response);
  originalFetch = globalThis.fetch;
  // @ts-expect-error - test stub
  globalThis.fetch = fetchMock;
});

afterEach(() => {
  if (originalFetch) globalThis.fetch = originalFetch;
  vi.clearAllMocks();
});


// ----- registration -------------------------------------------------------


describe("plugin registration", () => {
  test("subscribes to llm_input and llm_output", () => {
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error - register is on the definePluginEntry options
    luminPlugin.register(api);
    expect(typeof hooks.llm_input).toBe("function");
    expect(typeof hooks.llm_output).toBe("function");
    expect(api.logger.info).toHaveBeenCalledWith(
      expect.stringMatching(/subscribed to llm_input \+ llm_output/),
    );
  });

  test("degrades gracefully when api.on is missing", () => {
    const api = {
      pluginConfig: {},
      logger: { info: vi.fn(), warn: vi.fn() },
      // No `on` — simulate an older OpenClaw runtime.
    } as unknown as FakeApi;
    // @ts-expect-error - register is on the definePluginEntry options
    expect(() => luminPlugin.register(api)).not.toThrow();
    expect(api.logger.warn).toHaveBeenCalledWith(
      expect.stringMatching(/doesn't expose api\.on/),
    );
  });
});


// ----- end-to-end span emission ------------------------------------------


describe("span emission", () => {
  test("llm_input + llm_output produce a single span with content", async () => {
    const { api, hooks } = buildFakeApi({
      host: "http://lumin.test",
      project: "openclaw",
    });
    // @ts-expect-error
    luminPlugin.register(api);

    const runId = "run-abc";
    const trace = {
      traceId: "0123456789abcdef0123456789abcdef",
      spanId: "1111111111111111",
    };
    const ctx = { runId, trace };

    hooks.llm_input!(
      {
        runId,
        sessionId: "sess-1",
        provider: "ollama",
        model: "gpt-oss:120b-cloud",
        prompt: "what is npm",
        historyMessages: [
          { role: "user", content: [{ type: "text", text: "hi" }] },
        ],
      },
      ctx,
    );

    hooks.llm_output!(
      {
        runId,
        sessionId: "sess-1",
        provider: "ollama",
        model: "gpt-oss:120b-cloud",
        assistantTexts: ["npm is a package manager."],
        usage: { input: 42, output: 9 },
      },
      ctx,
    );

    // Let the fire-and-forget fetch settle.
    await new Promise((r) => setTimeout(r, 5));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://lumin.test/v1/spans");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Lumin-Project"]).toBe("openclaw");

    const body = JSON.parse(init.body);
    expect(body.spans).toHaveLength(1);
    const span = body.spans[0];
    expect(span.type).toBe("llm");
    expect(span.name).toBe("openclaw.llm");
    expect(span.model).toBe("gpt-oss:120b-cloud");
    expect(span.provider).toBe("ollama");
    expect(span.tokens_input).toBe(42);
    expect(span.tokens_output).toBe(9);
    expect(span.output).toBe("npm is a package manager.");
    // Input is JUST this turn's user prompt — not the conversation
    // history. The history count lives on metadata so operators can
    // still see how many prior turns replayed without the trace
    // ballooning into a chat log.
    expect(span.input).toBe("what is npm");
    expect(span.input).not.toContain("hi");
    expect(span.metadata["openclaw.history_message_count"]).toBe(1);
    // Trace stitching: trace_id derives from the inbound traceparent
    // hex, formatted as a UUID. The same input always produces the
    // same UUID — tests can assert on the deterministic shape.
    expect(span.trace_id).toBe("01234567-89ab-cdef-0123-456789abcdef");
  });

  test("thinking blocks are surfaced in metadata, not lost", async () => {
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error
    luminPlugin.register(api);

    hooks.llm_input!(
      { runId: "r1", sessionId: "s", provider: "ollama", model: "gpt-oss:120b-cloud",
        prompt: "hi", historyMessages: [] },
      undefined,
    );
    hooks.llm_output!(
      {
        runId: "r1",
        sessionId: "s",
        provider: "ollama",
        model: "gpt-oss:120b-cloud",
        assistantTexts: ["Hi Amit! How can I help you today?"],
        // Reasoning-model output: thinking block precedes the visible
        // text inside lastAssistant.content.
        lastAssistant: {
          role: "assistant",
          content: [
            { type: "thinking", thinking: "User just says hi. Respond simply." },
            { type: "text", text: "Hi Amit! How can I help you today?" },
          ],
        },
        usage: { input: 14505, output: 124 },
      },
      undefined,
    );

    await new Promise((r) => setTimeout(r, 5));
    const span = JSON.parse(fetchMock.mock.calls[0][1].body).spans[0];
    // Visible output stays clean — operators see what the user saw.
    expect(span.output).toBe("Hi Amit! How can I help you today?");
    // Thinking is not lost — it lives on metadata for drill-down.
    expect(span.metadata["openclaw.content.thinking"]).toBe(
      "User just says hi. Respond simply.",
    );
    expect(span.metadata["openclaw.thinking_chars"]).toBeGreaterThan(0);
  });

  test("non-reasoning model: no thinking metadata fields written", async () => {
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error
    luminPlugin.register(api);

    hooks.llm_input!(
      { runId: "r2", sessionId: "s", provider: "openai", model: "gpt-4o-mini",
        prompt: "hi", historyMessages: [] },
      undefined,
    );
    hooks.llm_output!(
      {
        runId: "r2",
        sessionId: "s",
        provider: "openai",
        model: "gpt-4o-mini",
        assistantTexts: ["Hi!"],
        // No thinking blocks, just a text reply.
        lastAssistant: {
          role: "assistant",
          content: [{ type: "text", text: "Hi!" }],
        },
        usage: { input: 3, output: 1 },
      },
      undefined,
    );

    await new Promise((r) => setTimeout(r, 5));
    const span = JSON.parse(fetchMock.mock.calls[0][1].body).spans[0];
    expect(span.metadata["openclaw.content.thinking"]).toBeUndefined();
    expect(span.metadata["openclaw.thinking_chars"]).toBeUndefined();
  });

  test("llm_output without prior llm_input still emits a span", async () => {
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error
    luminPlugin.register(api);

    hooks.llm_output!(
      {
        runId: "run-orphan",
        sessionId: "sess",
        provider: "openai",
        model: "gpt-4o-mini",
        assistantTexts: ["ok"],
        usage: { input: 5, output: 1 },
      },
      undefined,
    );

    await new Promise((r) => setTimeout(r, 5));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.spans[0].output).toBe("ok");
    expect(body.spans[0].input).toBeUndefined();
  });

  test("trace_id is deterministic from runId when ctx.trace is missing", async () => {
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error
    luminPlugin.register(api);

    const runId = "deterministic-run-id";
    const noTraceCtx = { runId };

    hooks.llm_input!(
      { runId, sessionId: "s", provider: "ollama", model: "m",
        prompt: "p", historyMessages: [] },
      noTraceCtx,
    );
    hooks.llm_output!(
      { runId, sessionId: "s", provider: "ollama", model: "m",
        assistantTexts: ["a"], usage: { input: 1, output: 1 } },
      noTraceCtx,
    );

    await new Promise((r) => setTimeout(r, 5));
    const traceA = JSON.parse(fetchMock.mock.calls[0][1].body).spans[0].trace_id;

    // Repeat with the same runId in a fresh fetch mock — id should match.
    fetchMock.mockClear();
    hooks.llm_input!(
      { runId, sessionId: "s", provider: "ollama", model: "m",
        prompt: "p", historyMessages: [] },
      noTraceCtx,
    );
    hooks.llm_output!(
      { runId, sessionId: "s", provider: "ollama", model: "m",
        assistantTexts: ["a"], usage: { input: 1, output: 1 } },
      noTraceCtx,
    );
    await new Promise((r) => setTimeout(r, 5));
    const traceB = JSON.parse(fetchMock.mock.calls[0][1].body).spans[0].trace_id;
    expect(traceB).toBe(traceA);
    expect(traceA).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });
});


// ----- failure handling ---------------------------------------------------


describe("error swallowing (Rule 7)", () => {
  test("fetch network failure does not throw out of the hook", async () => {
    fetchMock.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error
    luminPlugin.register(api);

    expect(() =>
      hooks.llm_output!(
        { runId: "r", sessionId: "s", provider: "p", model: "m",
          assistantTexts: ["a"], usage: { input: 1, output: 1 } },
        undefined,
      ),
    ).not.toThrow();

    // Let the rejection settle without raising an unhandled error.
    await new Promise((r) => setTimeout(r, 5));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("non-2xx Lumin response is logged but does not throw", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Server Error",
    } as Response);
    const { api, hooks } = buildFakeApi();
    // @ts-expect-error
    luminPlugin.register(api);

    expect(() =>
      hooks.llm_output!(
        { runId: "r", sessionId: "s", provider: "p", model: "m",
          assistantTexts: ["a"], usage: { input: 1, output: 1 } },
        undefined,
      ),
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 5));
  });
});
