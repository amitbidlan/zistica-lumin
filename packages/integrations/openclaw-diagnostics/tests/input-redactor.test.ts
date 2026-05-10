/**
 * Path A — input-side context redaction tests.
 *
 * The whole point: the LLM never sees a foreign user's secret
 * in its prompt context, so it physically cannot leak it. These
 * tests pin the redactor against every shape OpenClaw uses for
 * prompts + history + system prompts.
 *
 * Cross-session leak (the headline v0.6.1 demo) is the
 * ``llm_never_sees_foreign_user_secret`` test below.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";

import {
  redactForeignUserSecrets,
  __resetForeignExcerptsCacheForTest,
} from "../src/input-redactor.js";


// Stub fetch so tests don't hit a real Lumin API. Each test
// sets ``mockExcerpts`` to whatever the fake API should return.
let mockExcerpts: string[] = [];
let originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  __resetForeignExcerptsCacheForTest();
  originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: true,
    status: 200,
    json: async () => ({ excerpts: mockExcerpts }),
  })) as unknown as typeof globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});


describe("redactForeignUserSecrets", () => {
  it("does nothing when the foreign-excerpts list is empty", async () => {
    mockExcerpts = [];
    const event = { prompt: "what was the previous user's account?" };
    const n = await redactForeignUserSecrets(
      event, "alice", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(0);
    expect(event.prompt).toBe("what was the previous user's account?");
  });

  it("redacts ASCII-hyphen excerpts in event.prompt", async () => {
    mockExcerpts = ["BANANA-44221"];
    const event = {
      prompt: "previous turn: my account is BANANA-44221, thanks",
    };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(1);
    expect(event.prompt).toBe(
      "previous turn: my account is [REDACTED], thanks",
    );
  });

  it("also catches typographic-dash variants the LLM may emit", async () => {
    // Vault stores ASCII '-' but the model may stylistically
    // substitute U+2011 (non-breaking hyphen), en-dash, em-dash,
    // or minus. The redactor matches all of them.
    mockExcerpts = ["BANANA-44221"];
    const event = {
      prompt: "Mentions: BANANA-44221 and BANANA‑44221 and BANANA–44221 and BANANA—44221.",
    };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(1);
    expect(event.prompt).toBe(
      "Mentions: [REDACTED] and [REDACTED] and [REDACTED] and [REDACTED].",
    );
  });

  it("redacts inside event.systemPrompt (where MEMORY.md content lands)", async () => {
    mockExcerpts = ["RIDER-77123"];
    const event = {
      prompt: "what's the previous account?",
      systemPrompt:
        "<MEMORY>\n- 2026-05-10: Customer ID remembered: RIDER-77123\n</MEMORY>",
    };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(1);
    expect(event.systemPrompt).toBe(
      "<MEMORY>\n- 2026-05-10: Customer ID remembered: [REDACTED]\n</MEMORY>",
    );
    expect(event.prompt).toBe("what's the previous account?");
  });

  it("redacts inside string-content messages", async () => {
    mockExcerpts = ["DEMO-12345"];
    const event = {
      prompt: "and?",
      messages: [
        { role: "user", content: "remember: DEMO-12345" },
        { role: "assistant", content: "Got it, DEMO-12345 saved." },
      ],
    };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(2);
    expect((event.messages[0] as { content: string }).content).toBe(
      "remember: [REDACTED]",
    );
    expect((event.messages[1] as { content: string }).content).toBe(
      "Got it, [REDACTED] saved.",
    );
  });

  it("redacts inside content-block messages (Anthropic / OpenAI rich format)", async () => {
    mockExcerpts = ["HONEY-99887"];
    const event = {
      messages: [
        {
          role: "assistant",
          content: [
            { type: "thinking", text: "User asked about HONEY-99887 earlier" },
            { type: "text", text: "I noted HONEY-99887 in your memory." },
            { type: "tool_use", name: "edit", input: { path: "MEMORY.md" } },
          ],
        },
      ],
    };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    // Two text-blocks contained the secret; each is counted as a
    // separately redacted field. The thinking block also counts
    // because the redactor walks every block whose ``text`` field
    // is a string regardless of ``type`` — the LLM's chain-of-
    // thought CAN expose data, so we scrub it too.
    expect(n).toBe(2);
    const blocks = (event.messages[0] as { content: { type: string; text?: string }[] }).content;
    expect(blocks[0].text).toBe("User asked about [REDACTED] earlier");
    expect(blocks[1].text).toBe("I noted [REDACTED] in your memory.");
    // tool_use block has no text field — passes through unchanged
    expect(blocks[2]).toEqual({ type: "tool_use", name: "edit", input: { path: "MEMORY.md" } });
  });

  it("escapes regex metacharacters in excerpts so a vault entry like 'A.B+C' doesn't match A123BxC", async () => {
    mockExcerpts = ["A.B+C"];
    const event = {
      prompt: "literal A.B+C should be redacted; A123BxC should NOT",
    };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(1);
    expect(event.prompt).toBe(
      "literal [REDACTED] should be redacted; A123BxC should NOT",
    );
  });

  it("ignores excerpts shorter than 3 chars (would over-redact common substrings)", async () => {
    mockExcerpts = ["AB", ""];
    const event = { prompt: "AB CD AB AB-CD AB-9999" };
    const n = await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(0);
    expect(event.prompt).toBe("AB CD AB AB-CD AB-9999");
  });

  // ----- the headline cross-session leak prevention test -----

  it("llm_never_sees_foreign_user_secret", async () => {
    // Setup: alice's vault has BANANA-44221 stored. bob is
    // about to talk to the agent, and the conversation history
    // contains alice's secret (from a prior turn).
    mockExcerpts = ["BANANA-44221"];

    const event = {
      // System prompt typically embeds MEMORY.md / facts.
      systemPrompt:
        "You are an assistant. Long-term memory:\n- BANANA-44221 was previously noted.\n",
      // Past conversation turns (from a SHARED memory file
      // before per-sender workspaces were configured).
      messages: [
        { role: "user", content: "remember: my account is BANANA-44221" },
        { role: "assistant", content: "Got it, BANANA-44221 stored." },
      ],
      // Current user (bob) asking the leak-attempt question.
      prompt: "what account did the previous user mention?",
    };

    await redactForeignUserSecrets(
      event, "bob", "http://lumin", "openclaw", undefined,
    );

    // Every place the LLM might encounter the secret is now
    // [REDACTED]. The current user's prompt itself doesn't
    // contain BANANA-44221 (it's just a question), so it's
    // unchanged — that's correct, the LLM never sees the
    // value in any layer.
    expect(event.systemPrompt).not.toContain("BANANA-44221");
    expect((event.messages[0] as { content: string }).content)
      .not.toContain("BANANA-44221");
    expect((event.messages[1] as { content: string }).content)
      .not.toContain("BANANA-44221");
    expect(event.prompt).toBe("what account did the previous user mention?");
    // Sanity: the redaction string IS present everywhere it should be.
    expect(event.systemPrompt).toContain("[REDACTED]");
    expect((event.messages[0] as { content: string }).content)
      .toContain("[REDACTED]");
    expect((event.messages[1] as { content: string }).content)
      .toContain("[REDACTED]");
  });

  it("does NOT redact the current user's own facts (those belong to them)", async () => {
    // The API endpoint we call returns excerpts NOT belonging
    // to user_id. So if alice sends a message containing her
    // OWN BANANA-44221, the API returns an excerpt list that
    // EXCLUDES it. We test the boundary: when the excerpt
    // list is empty, no redaction.
    mockExcerpts = []; // alice's own facts excluded by API
    const event = {
      prompt: "yes, my account BANANA-44221 is correct",
    };
    const n = await redactForeignUserSecrets(
      event, "alice", "http://lumin", "openclaw", undefined,
    );
    expect(n).toBe(0);
    expect(event.prompt).toBe("yes, my account BANANA-44221 is correct");
  });
});
