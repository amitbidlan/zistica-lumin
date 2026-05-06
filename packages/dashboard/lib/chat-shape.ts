/**
 * Detect chat-shaped traces and parse their content.
 *
 * Most Lumin integrations are *task-shaped* — one input → one output,
 * no surrounding conversation. But OpenClaw (and any framework that
 * runs over Telegram / Slack / Discord) is *chat-shaped*: every trace
 * is one turn in an ongoing conversation, and the model sees prior
 * history every turn.
 *
 * The trace detail page picks a renderer based on the answer to
 * `isChatShapedTrace(...)`. This avoids per-framework UI forks —
 * future chat-style integrations (Mastra-chat, VoltAgent-chat) reuse
 * the same chat view as long as they emit the same metadata signals.
 *
 * Signals we look for, ranked by strength:
 *   1. ``openclaw.history_message_count`` on any span — strongest
 *      signal, written by ``@lumin-io/openclaw-diagnostics`` at
 *      every turn.
 *   2. Presence of ``chat_id`` (Telegram / Slack / Discord conventions)
 *      anywhere in the input — survives integrations that don't yet
 *      emit the structured metadata field.
 *   3. ``span.session_id`` set AND a known chat provider name on the
 *      trace — fallback for purely heuristic detection.
 *
 * Pure functions only — no React, no I/O. Component renders the
 * result; the helper just classifies and parses.
 */

import { Span, Trace } from './api';


// ----- detection --------------------------------------------------------


export function isChatShapedTrace(trace: Trace, spans: Span[]): boolean {
  for (const s of spans) {
    if (looksChatShapedFromMetadata(s.metadata)) return true;
  }
  // Trace-level metadata fallback (the trace row's own metadata).
  if (looksChatShapedFromMetadata(trace.metadata)) return true;
  // Heuristic: a spans list with an openclaw-prefixed span name and
  // a session_id is chat-shaped even if the metadata didn't survive.
  const hasOpenClawSpan = spans.some(
    (s) => (s.name ?? '').startsWith('openclaw'),
  );
  if (hasOpenClawSpan && trace.session_id) return true;
  return false;
}

function looksChatShapedFromMetadata(metadata: unknown): boolean {
  if (!metadata || typeof metadata !== 'object') return false;
  const md = metadata as Record<string, unknown>;
  if (typeof md['openclaw.history_message_count'] === 'number') return true;
  if (typeof md['chat.history_count'] === 'number') return true;
  // Also accept the presence of a ``chat_id`` key under nested
  // ``conversation`` or ``sender`` metadata.
  const conv = md.conversation;
  if (conv && typeof conv === 'object' && 'chat_id' in (conv as object)) {
    return true;
  }
  return false;
}


// ----- input parsing ----------------------------------------------------
//
// OpenClaw wraps every user prompt with a verbose metadata header:
//
//   Conversation info (untrusted metadata):
//   ```json
//   { "chat_id": "telegram:...", "message_id": "...", ... }
//   ```
//
//   Sender (untrusted metadata):
//   ```json
//   { "label": "...", "id": "...", "name": "..." }
//   ```
//
//   <actual user text>
//
// In the generic span detail view operators see the whole wall. The
// chat view parses it apart so the bubble shows just the user's
// actual text, with conversation+sender metadata rendered as a small
// header.

export interface ParsedChatPrompt {
  conversation?: Record<string, unknown>;
  sender?: Record<string, unknown>;
  userText: string;
}


export function parseOpenClawPrompt(prompt: string | null | undefined): ParsedChatPrompt {
  if (!prompt) return { userText: '' };

  // Pull out the two named JSON blocks. Match either ``json fenced
  // blocks (most common) or a bare JSON object on the line below the
  // header. The regex tolerates extra whitespace + supports both
  // labels in either order.
  const blocks = extractLabeledJsonBlocks(prompt);
  let remaining = prompt;
  for (const b of blocks) {
    remaining = remaining.replace(b.fullMatch, '');
  }
  // After stripping, collapse repeated blank lines and trim.
  const userText = remaining.replace(/\n{3,}/g, '\n\n').trim();

  return {
    conversation: blocks.find((b) => /conversation/i.test(b.label))?.parsed,
    sender: blocks.find((b) => /sender/i.test(b.label))?.parsed,
    userText: userText || prompt.trim(),
  };
}


interface LabeledBlock {
  label: string;
  parsed: Record<string, unknown> | undefined;
  fullMatch: string;
}


function extractLabeledJsonBlocks(text: string): LabeledBlock[] {
  // Match: <label> (untrusted metadata):\n```json\n{...}\n```
  const re = /([A-Za-z][A-Za-z ]+?)\s*\(untrusted metadata\):\s*```json\s*([\s\S]*?)```/g;
  const out: LabeledBlock[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    const label = m[1].trim();
    const body = m[2].trim();
    let parsed: Record<string, unknown> | undefined;
    try {
      const v = JSON.parse(body);
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        parsed = v as Record<string, unknown>;
      }
    } catch {
      // Leave parsed undefined; the chat view falls back to the raw text.
    }
    out.push({ label, parsed, fullMatch: m[0] });
  }
  return out;
}


// ----- assistant content extraction --------------------------------------


export interface ChatAssistantParts {
  thinking?: string;
  text?: string;
}


/**
 * Extract the assistant's reasoning + visible reply for chat
 * rendering. Two sources, in priority order:
 *   1. ``metadata.openclaw.content.thinking`` — the structured field
 *      our plugin writes. Cleanest, no parsing needed.
 *   2. The span's plain ``output`` field as the visible text.
 */
export function extractAssistantParts(span: Span): ChatAssistantParts {
  const md = (span.metadata ?? {}) as Record<string, unknown>;
  const thinking = typeof md['openclaw.content.thinking'] === 'string'
    ? (md['openclaw.content.thinking'] as string)
    : typeof md['content.thinking'] === 'string'
      ? (md['content.thinking'] as string)
      : undefined;
  const text = span.output ?? undefined;
  return { thinking, text };
}
