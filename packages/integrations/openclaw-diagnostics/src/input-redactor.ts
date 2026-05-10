/**
 * Path A — input-side context redaction.
 *
 * Cross-session leaks happen because the LLM has the foreign
 * user's secret in its prompt context (history, system prompt,
 * MEMORY.md content, etc.). Output-side rewriting is
 * fundamentally leaky on Slack — ``chat.postMessage`` always
 * dispatches the original first and ``chat.update`` redacts a
 * second later, so notifications, lock-screens, and the first
 * ~500ms of the recipient's UI all show the secret.
 *
 * The architecturally correct fix: scrub every foreign-user
 * vault excerpt out of the prompt BEFORE it reaches the LLM.
 * The LLM physically cannot leak what isn't in its context.
 *
 * Hook used: ``before_prompt_build``. It IS async-aware AND
 * awaited by OpenClaw, and the event object exposes mutable
 * references to ``prompt``, ``systemPrompt``, and
 * ``messages[*]``. We mutate them in place — there's no
 * supported "return new prompt" shape, so in-place mutation is
 * how we actually affect the LLM's input.
 *
 * Performance: one HTTP GET per turn to fetch the foreign-
 * excerpts list (cached for ~5s). Replacement is a single
 * regex pass per text field. Net overhead < 50 ms on a warm
 * cache and < 200 ms on a cold one — well under the 15 s
 * before_prompt_build budget OpenClaw enforces.
 */


// Cache: user_id -> { excerpts, fetchedAtMs }. TTL keeps us
// from hitting the API on every turn while staying fresh
// enough that a newly-vaulted secret gets scrubbed within a
// couple of seconds.
const FOREIGN_EXCERPTS_TTL_MS = 5_000;

const foreignExcerptsCache = new Map<
  string,
  { excerpts: string[]; fetchedAtMs: number }
>();


// Compose a regex that matches the excerpt OR any of its
// typographic-dash variants (the vault stores ASCII '-' but
// the LLM may emit U+2011 / en-dash / em-dash / minus).
// Without this the redactor would fail the moment the model
// stylistically substitutes a fancy hyphen.
function buildExcerptRegex(excerpt: string): RegExp | undefined {
  if (!excerpt || excerpt.length < 3) return undefined;
  // Escape regex metacharacters EXCEPT '-' — we leave it raw
  // so the next step can swap it for a dash-variant class.
  // ('-' isn't a metachar outside of character classes, so
  // omitting it from the escape set is safe.)
  const escaped = excerpt.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Replace ASCII '-' with a class that matches every common
  // dash variant: ASCII hyphen, underscore-as-separator, and
  // U+2010 / U+2011 / U+2012 / U+2013 / U+2014 / U+2015 /
  // U+2212. The leading '-' inside the class is literal.
  // Without this step the redactor missed BANANA‑44221
  // (U+2011) on live tests.
  const withDashClass = escaped.replace(/-/g, "[-_‐‑‒–—―−]");
  try {
    return new RegExp(withDashClass, "g");
  } catch {
    return undefined;
  }
}


async function fetchForeignExcerpts(
  userId: string,
  host: string,
  project: string,
  log: { info?: (s: string) => void; warn?: (s: string) => void } | undefined,
): Promise<string[]> {
  const cached = foreignExcerptsCache.get(userId);
  if (cached && Date.now() - cached.fetchedAtMs < FOREIGN_EXCERPTS_TTL_MS) {
    return cached.excerpts;
  }
  const url = `${host.replace(/\/+$/, "")}/v1/firewall/vault/foreign-excerpts` +
    `?user_id=${encodeURIComponent(userId)}`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 5000);
  try {
    const resp = await fetch(url, {
      method: "GET",
      headers: { "X-Lumin-Project": project },
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      log?.warn?.(
        `lumin-diagnostics: foreign-excerpts fetch failed (http_${resp.status})`,
      );
      // Negative-cache failures briefly so a flapping API doesn't
      // trigger a fetch on every turn.
      foreignExcerptsCache.set(userId, {
        excerpts: cached?.excerpts ?? [],
        fetchedAtMs: Date.now(),
      });
      return cached?.excerpts ?? [];
    }
    const body = (await resp.json()) as { excerpts?: string[] };
    const excerpts = Array.isArray(body.excerpts) ? body.excerpts : [];
    foreignExcerptsCache.set(userId, {
      excerpts,
      fetchedAtMs: Date.now(),
    });
    return excerpts;
  } catch (err) {
    log?.warn?.(
      `lumin-diagnostics: foreign-excerpts fetch errored: ${(err as Error).message}`,
    );
    return cached?.excerpts ?? [];
  } finally {
    clearTimeout(timer);
  }
}


/**
 * Walk a content block array (the
 * ``[{ type: "text", text: "..." }, { type: "thinking", text: "..." }, ...]``
 * shape) and return a copy with each text element redacted.
 * Falls back to returning the original array unchanged when
 * the content shape isn't recognised.
 */
function redactContentBlocks(
  content: unknown[],
  regexes: RegExp[],
): unknown[] {
  let mutated = false;
  const result = content.map((block) => {
    if (
      block
      && typeof block === "object"
      && typeof (block as { text?: unknown }).text === "string"
    ) {
      const orig = (block as { text: string }).text;
      const redacted = applyRedactions(orig, regexes);
      if (redacted !== orig) {
        mutated = true;
        return { ...(block as object), text: redacted };
      }
    }
    return block;
  });
  return mutated ? result : content;
}


function applyRedactions(text: string, regexes: RegExp[]): string {
  let out = text;
  for (const rx of regexes) {
    rx.lastIndex = 0;  // global regex state is per-instance; reset
    out = out.replace(rx, "[REDACTED]");
  }
  return out;
}


/**
 * Send the entire prompt + history to the API's redact-context
 * endpoint, which does BOTH known-vault-excerpt matching AND
 * structural-pattern detection (covers secrets that were never
 * recorded in the vault — e.g. because user_id was empty when
 * ingested, but the LLM still has them in conversation history).
 *
 * Returns the redacted texts in the same order, or undefined
 * on failure (caller falls back to the simpler client-side
 * vault-excerpt-only redaction).
 */
async function callRedactContextEndpoint(
  userId: string,
  texts: string[],
  host: string,
  project: string,
  log: { info?: (s: string) => void; warn?: (s: string) => void } | undefined,
  detectors?: {
    vault_exact?: boolean;
    structural_pattern?: boolean;
    presidio?: boolean;
  },
): Promise<string[] | undefined> {
  if (texts.length === 0) return [];
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const url = `${host.replace(/\/+$/, "")}/v1/firewall/redact-context`;
    // Body includes optional ``detectors`` toggles (Slice 3). Server
    // treats undefined as "all on"; explicit false skips the detector
    // for this call. Operators wire these via the active
    // securityProfile or per-layer ``l3Detectors`` config.
    const reqBody: Record<string, unknown> = { user_id: userId, texts };
    if (detectors) reqBody.detectors = detectors;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lumin-Project": project,
      },
      body: JSON.stringify(reqBody),
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      log?.warn?.(
        `lumin-diagnostics: redact-context HTTP ${resp.status}`,
      );
      return undefined;
    }
    const body = (await resp.json()) as { redacted?: string[] };
    if (!Array.isArray(body.redacted) || body.redacted.length !== texts.length) {
      return undefined;
    }
    return body.redacted;
  } catch (err) {
    log?.warn?.(
      `lumin-diagnostics: redact-context errored: ${(err as Error).message}`,
    );
    return undefined;
  } finally {
    clearTimeout(timer);
  }
}


/**
 * Public entry point — call this from before_prompt_build with
 * the raw event object. Mutates ``event.prompt``,
 * ``event.systemPrompt``, and every message's content in
 * ``event.messages`` in place. Returns the count of fields
 * that actually changed (for logging only).
 *
 * The redaction strategy collects every text field the LLM is
 * about to see, sends them to the Lumin API in one batch,
 * receives back the redacted versions, and writes them back in
 * place. The server-side endpoint catches BOTH known foreign
 * vault entries AND structured-ID patterns that aren't in the
 * current user's own vault — the latter closes the gap where
 * a foreign secret was leaked before user_id propagation
 * started working, so it never landed in the vault but is
 * still in the LLM's conversation history.
 */
export async function redactForeignUserSecrets(
  event: {
    prompt?: string;
    systemPrompt?: string;
    messages?: unknown[];
  },
  userId: string,
  host: string,
  project: string,
  log: { info?: (s: string) => void; warn?: (s: string) => void } | undefined,
  // Slice 3: optional detector toggles forwarded to the server. When
  // undefined, server applies its defaults (all detectors on).
  detectors?: {
    vault_exact?: boolean;
    structural_pattern?: boolean;
    presidio?: boolean;
  },
): Promise<number> {
  // ---- 1. Collect every text field the LLM is about to see -----
  // We build a list of (key, getter, setter) tuples so we can
  // round-trip the redactions back into the exact same fields.
  type Field = {
    label: string;
    get: () => string;
    set: (s: string) => void;
  };
  const fields: Field[] = [];

  if (typeof event.prompt === "string") {
    fields.push({
      label: "prompt",
      get: () => event.prompt as string,
      set: (s) => { event.prompt = s; },
    });
  }
  if (typeof event.systemPrompt === "string") {
    fields.push({
      label: "systemPrompt",
      get: () => event.systemPrompt as string,
      set: (s) => { event.systemPrompt = s; },
    });
  }
  if (Array.isArray(event.messages)) {
    for (let i = 0; i < event.messages.length; i++) {
      const msg = event.messages[i] as { content?: unknown } | null;
      if (!msg || typeof msg !== "object") continue;
      const c = msg.content;
      if (typeof c === "string") {
        const idx = i;
        fields.push({
          label: `messages[${idx}].content`,
          get: () => (event.messages?.[idx] as { content: string }).content,
          set: (s) => {
            (event.messages?.[idx] as { content: string }).content = s;
          },
        });
      } else if (Array.isArray(c)) {
        for (let j = 0; j < c.length; j++) {
          const block = c[j] as { type?: string; text?: unknown } | null;
          if (
            block
            && typeof block === "object"
            && typeof block.text === "string"
          ) {
            const i_ = i;
            const j_ = j;
            fields.push({
              label: `messages[${i_}].content[${j_}].text`,
              get: () => {
                const blk = (event.messages?.[i_] as {
                  content: { text: string }[];
                }).content[j_];
                return blk.text;
              },
              set: (s) => {
                const blk = (event.messages?.[i_] as {
                  content: { text: string }[];
                }).content[j_];
                blk.text = s;
              },
            });
          }
        }
      }
    }
  }

  if (fields.length === 0) return 0;

  // ---- 2. Send to the server-side redactor (vault + pattern) -----
  const originals = fields.map((f) => f.get());
  const redacted = await callRedactContextEndpoint(
    userId, originals, host, project, log, detectors,
  );

  // ---- 3. Fallback: client-side vault-excerpt redaction only -----
  // If the redact-context endpoint failed (older API, network
  // hiccup), fall back to the simpler client-side regex pass
  // against vault excerpts. We still scrub known secrets even
  // if pattern detection isn't available.
  const out: string[] = redacted ?? await (async () => {
    const excerpts = await fetchForeignExcerpts(userId, host, project, log);
    if (excerpts.length === 0) return originals;
    const regexes = excerpts
      .map(buildExcerptRegex)
      .filter((r): r is RegExp => r !== undefined);
    if (regexes.length === 0) return originals;
    return originals.map((t) => applyRedactions(t, regexes));
  })();

  // ---- 4. Write redactions back into the event in place ---------
  let redactedCount = 0;
  for (let i = 0; i < fields.length; i++) {
    if (out[i] !== originals[i]) {
      fields[i].set(out[i]);
      redactedCount++;
    }
  }

  if (redactedCount > 0) {
    log?.info?.(
      `lumin-diagnostics: input-redactor scrubbed ` +
      `${redactedCount}/${fields.length} field(s) of foreign secrets ` +
      `(user_id=${userId} mode=${redacted ? "server-redact" : "vault-only"})`,
    );
  }

  return redactedCount;
}


// Test-only: clear the in-process cache. Lets unit tests
// avoid having to wait out the TTL. Not exported in the
// public surface — vitest just imports it directly.
export function __resetForeignExcerptsCacheForTest(): void {
  foreignExcerptsCache.clear();
}
