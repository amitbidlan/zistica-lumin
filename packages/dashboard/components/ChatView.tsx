'use client';

/**
 * Chat-shaped trace renderer.
 *
 * Presents a single trace as a *one-turn conversation*: sender header,
 * user bubble, optional thinking accordion, assistant bubble. This is
 * the right view when the underlying agent is conversational
 * (OpenClaw, Telegram bots, Slack apps) — operators recognize the
 * shape immediately and don't have to mentally translate from
 * function-call-shape (Input → Output) into chat-shape.
 *
 * The component is fed a single span (the LLM call) plus the parent
 * trace. Conversation history isn't shown here because it's not on
 * the span — it's distributed across other traces in the same
 * session. A future iteration can fetch /v1/sessions/{id}/traces
 * and render a multi-turn thread.
 */

import { useState } from 'react';
import { Span, Trace } from '@/lib/api';
import {
  ChatAssistantParts,
  extractAssistantParts,
  parseOpenClawPrompt,
} from '@/lib/chat-shape';


export default function ChatView({
  trace,
  spans,
}: {
  trace: Trace;
  spans: Span[];
}) {
  // The "headline" span for chat view is the LLM call. There's
  // usually only one per trace under our plugin; if multiple, take
  // the first ``llm``-typed span.
  const headline = pickHeadlineSpan(spans);
  if (!headline) {
    return (
      <div className="text-[var(--muted)] text-sm">
        No LLM span on this trace — chat view needs at least one model call.
      </div>
    );
  }
  const parsed = parseOpenClawPrompt(headline.input);
  const assistant = extractAssistantParts(headline);

  return (
    <div className="space-y-4">
      <SenderHeader
        conversation={parsed.conversation}
        sender={parsed.sender}
        startedAt={trace.started_at}
      />
      <div className="space-y-3">
        <UserBubble text={parsed.userText} />
        <AssistantBubble parts={assistant} />
      </div>
      <TurnFooter span={headline} />
    </div>
  );
}


function pickHeadlineSpan(spans: Span[]): Span | undefined {
  // ChatView is only meaningful when there's an actual model call —
  // its renderer parses the input as a user prompt and the output as
  // an assistant reply. Falling back to a tool-only span would feed
  // tool params (raw JSON) to the user-bubble renderer, producing
  // nonsense. Return undefined when no LLM span is present so the
  // caller's "No LLM span on this trace" message kicks in and the
  // trace falls back to the task-shape default panels.
  return spans.find((s) => s.type === 'llm');
}


// ----- pieces -----------------------------------------------------------


function SenderHeader({
  conversation,
  sender,
  startedAt,
}: {
  conversation?: Record<string, unknown>;
  sender?: Record<string, unknown>;
  startedAt: string | null;
}) {
  const senderName =
    pickString(sender, 'name') ??
    pickString(sender, 'label') ??
    pickString(conversation, 'sender');
  const channel = pickString(conversation, 'chat_id');
  const messageId = pickString(conversation, 'message_id');
  const ts = pickString(conversation, 'timestamp');

  if (!senderName && !channel) return null;
  return (
    <div className="border border-[var(--border)] rounded p-3 flex items-center justify-between text-xs">
      <div className="flex items-center gap-3">
        <Avatar name={senderName ?? 'unknown'} />
        <div>
          <div className="font-mono text-sm">{senderName ?? 'Unknown sender'}</div>
          {channel ? (
            <div className="text-[var(--muted)] font-mono text-[11px]">
              via {channel}
            </div>
          ) : null}
        </div>
      </div>
      <div className="text-[var(--muted)] font-mono text-[11px] text-right">
        {ts ? <div>{ts}</div> : null}
        {messageId ? <div>msg #{messageId}</div> : null}
        {!ts && startedAt ? <div>{startedAt}</div> : null}
      </div>
    </div>
  );
}


function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      {/* ``--accent-glow`` is the subtle accent-tinted background that
          flips between dark and light mode — keeps user bubbles
          distinguishable from assistant bubbles in both themes
          without being shouty. */}
      <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-[var(--accent-glow)] border border-[var(--border)] px-4 py-2.5 text-sm whitespace-pre-wrap break-words">
        <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
          User
        </div>
        {text || <span className="italic text-[var(--muted)]">(empty)</span>}
      </div>
    </div>
  );
}


function AssistantBubble({ parts }: { parts: ChatAssistantParts }) {
  // Default-collapsed thinking — reasoning traces are long and most
  // operators want to see the reply first. Click to expand.
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const hasThinking = parts.thinking && parts.thinking.length > 0;

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] space-y-2">
        {hasThinking ? (
          // Theme-aware reasoning accordion. The violet hue + low-
          // opacity wash + deep-violet text stays readable in both
          // light and dark mode because the colors come from
          // ``--thinking-*`` tokens that flip with ``data-theme``.
          // Pre-fix this used Tailwind's ``violet-950`` and
          // ``violet-100`` directly, which collapsed to lavender-on-
          // pale-violet in light mode and disappeared.
          <div
            className="rounded-2xl rounded-bl-sm border overflow-hidden"
            style={{
              borderColor: 'var(--thinking-border)',
              background: 'var(--thinking-bg)',
            }}
          >
            <button
              type="button"
              onClick={() => setThinkingOpen(!thinkingOpen)}
              className="w-full px-4 py-2 flex items-center gap-2 text-xs transition-colors hover:[background:var(--thinking-bg-hover)]"
              style={{ color: 'var(--thinking-fg)' }}
              aria-expanded={thinkingOpen}
            >
              <span aria-hidden>🧠</span>
              <span className="uppercase tracking-wider">Reasoning</span>
              <span className="text-[var(--muted)] font-mono ml-1">
                {parts.thinking!.length.toLocaleString()} chars
              </span>
              <span className="ml-auto font-mono">{thinkingOpen ? '−' : '+'}</span>
            </button>
            {thinkingOpen ? (
              <pre
                className="px-4 pb-3 text-[12px] font-mono whitespace-pre-wrap break-words border-t"
                style={{
                  color: 'var(--thinking-fg)',
                  borderColor: 'var(--thinking-border)',
                }}
              >
                {parts.thinking}
              </pre>
            ) : null}
          </div>
        ) : null}
        <div className="rounded-2xl rounded-bl-sm bg-[var(--background-raised)] border border-[var(--border)] px-4 py-2.5 text-sm whitespace-pre-wrap break-words">
          <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
            Assistant
          </div>
          {parts.text || <span className="italic text-[var(--muted)]">(no reply)</span>}
        </div>
      </div>
    </div>
  );
}


function TurnFooter({ span }: { span: Span }) {
  const md = (span.metadata ?? {}) as Record<string, unknown>;
  const historyCount = typeof md['openclaw.history_message_count'] === 'number'
    ? (md['openclaw.history_message_count'] as number)
    : undefined;
  const sysPromptChars = typeof md['openclaw.system_prompt_chars'] === 'number'
    ? (md['openclaw.system_prompt_chars'] as number)
    : undefined;
  const thinkingChars = typeof md['openclaw.thinking_chars'] === 'number'
    ? (md['openclaw.thinking_chars'] as number)
    : undefined;

  return (
    <div className="border-t border-[var(--border)] pt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-[11px] text-[var(--muted)] font-mono">
      <span>{span.model ?? 'unknown model'}</span>
      {span.tokens_input != null || span.tokens_output != null ? (
        <span>
          {span.tokens_input ?? 0}/{span.tokens_output ?? 0} tok
        </span>
      ) : null}
      {span.duration_ms != null ? <span>{span.duration_ms} ms</span> : null}
      {historyCount !== undefined ? <span>{historyCount} prior messages</span> : null}
      {sysPromptChars !== undefined ? <span>{sysPromptChars.toLocaleString()} sysprompt chars</span> : null}
      {thinkingChars !== undefined ? <span>{thinkingChars.toLocaleString()} thinking chars</span> : null}
    </div>
  );
}


function Avatar({ name }: { name: string }) {
  // First letter of each word, max 2.
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .join('');
  return (
    <div
      aria-hidden
      className="h-8 w-8 rounded-full bg-[var(--background-hover)] border border-[var(--border)] flex items-center justify-center text-xs font-mono"
    >
      {initials || '?'}
    </div>
  );
}


function pickString(
  obj: Record<string, unknown> | undefined,
  key: string,
): string | undefined {
  if (!obj) return undefined;
  const v = obj[key];
  return typeof v === 'string' && v.length > 0 ? v : undefined;
}
