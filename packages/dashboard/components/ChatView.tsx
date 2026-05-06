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
  // Tool spans for this trace, ordered by start time so the chat view
  // shows them in the order the agent actually invoked them.
  const tools = spans
    .filter((s) => s.type === 'tool')
    .slice()
    .sort((a, b) => (a.started_at ?? '').localeCompare(b.started_at ?? ''));

  return (
    <div className="space-y-4">
      <SenderHeader
        conversation={parsed.conversation}
        sender={parsed.sender}
        startedAt={trace.started_at}
      />
      <div className="space-y-3">
        <UserBubble text={parsed.userText} />
        <AssistantWork
          parts={assistant}
          tools={tools}
        />
      </div>
      <TurnFooter span={headline} tools={tools} />
    </div>
  );
}


function pickHeadlineSpan(spans: Span[]): Span | undefined {
  // Prefer an llm-typed span; fall back to the first span overall.
  return spans.find((s) => s.type === 'llm') ?? spans[0];
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


function AssistantWork({
  parts,
  tools,
}: {
  parts: ChatAssistantParts;
  tools: Span[];
}) {
  // Default-collapsed reasoning — long traces get out of the way of
  // the visible reply, which is what most operators are scanning for.
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const hasThinking = parts.thinking && parts.thinking.length > 0;

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] space-y-2">
        {hasThinking ? (
          // Theme-aware reasoning accordion. Same ``--thinking-*``
          // tokens flip across light/dark so the violet stays
          // readable in both. See the SpanRow.tsx fallback for the
          // /spans timeline equivalent.
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
        {/* Tool invocations rendered between reasoning and the
            visible reply — that's the temporal order the agent
            actually executes (think → search → fetch → answer).
            Each tool gets its own collapsible accordion with the
            input/output JSON inline. */}
        {tools.map((t) => (
          <ToolAccordion key={t.id} span={t} />
        ))}
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


// Per-tool icon. Falls back to a generic wrench for tools we don't
// recognize. The icon is purely cosmetic — the tool name is always
// shown next to it for unambiguous identification.
function toolIcon(toolName: string): string {
  const n = toolName.toLowerCase();
  if (n.includes('search')) return '🔎';
  if (n.includes('fetch') || n.includes('http') || n.includes('web')) return '🌐';
  if (n.includes('file') || n.includes('read') || n.includes('write')) return '📄';
  if (n.includes('exec') || n.includes('shell') || n.includes('run')) return '💻';
  if (n.includes('code')) return '💻';
  if (n.includes('memory') || n.includes('recall')) return '🧠';
  if (n.includes('image') || n.includes('vision')) return '🖼️';
  return '🔧';
}


function formatDurationShort(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}


function prettyJsonOrText(s: string | null | undefined): string {
  if (s == null || s === '') return '(empty)';
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch {
    return s;
  }
}


function ToolAccordion({ span }: { span: Span }) {
  const [open, setOpen] = useState(false);
  const isError = span.status === 'error';
  const name = span.tool_name ?? span.name ?? 'tool';
  const dur = formatDurationShort(span.duration_ms);
  // Same neutral surface as the assistant bubble so tools read as
  // part of the bot's work. Error tools get a red accent so they
  // stand out without needing to expand.
  const baseBorder = isError ? 'rgba(239, 68, 68, 0.45)' : 'var(--border)';
  const baseBg = isError ? 'rgba(239, 68, 68, 0.08)' : 'var(--background-raised)';
  return (
    <div
      className="rounded-2xl rounded-bl-sm border overflow-hidden"
      style={{ borderColor: baseBorder, background: baseBg }}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full px-4 py-2 flex items-center gap-2 text-xs transition-colors hover:bg-[var(--background-hover)]"
        aria-expanded={open}
      >
        <span aria-hidden>{toolIcon(name)}</span>
        <span className="font-mono">{name}</span>
        <span className="text-[var(--muted)] font-mono ml-1">{dur}</span>
        <span
          className={
            isError
              ? 'text-[10px] uppercase tracking-wider px-1.5 py-0.5 border rounded text-red-300 border-red-700 bg-red-900/30'
              : 'text-[10px] uppercase tracking-wider text-[var(--muted)]'
          }
        >
          {isError ? 'error' : 'ok'}
        </span>
        <span className="ml-auto font-mono text-[var(--muted)]">{open ? '−' : '+'}</span>
      </button>
      {open ? (
        <div
          className="px-4 pb-3 pt-1 text-[11px] space-y-2 border-t"
          style={{ borderColor: 'var(--border)' }}
        >
          {span.input != null && span.input !== '' ? (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
                Input
              </div>
              <pre className="font-mono whitespace-pre-wrap break-all border border-[var(--border)] rounded p-2 max-h-64 overflow-auto">
                {prettyJsonOrText(span.input)}
              </pre>
            </div>
          ) : null}
          {span.output != null && span.output !== '' ? (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--muted)] mb-1">
                Output
              </div>
              <pre className="font-mono whitespace-pre-wrap break-all border border-[var(--border)] rounded p-2 max-h-64 overflow-auto">
                {prettyJsonOrText(span.output)}
              </pre>
            </div>
          ) : null}
          {span.error_message ? (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-red-400 mb-1">
                Error
              </div>
              <pre className="font-mono whitespace-pre-wrap break-all border border-red-900/50 bg-red-950/20 rounded p-2 text-red-200">
                {span.error_message}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}


function TurnFooter({ span, tools }: { span: Span; tools: Span[] }) {
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
  const toolErrors = tools.filter((t) => t.status === 'error').length;

  return (
    <div className="border-t border-[var(--border)] pt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-[11px] text-[var(--muted)] font-mono">
      <span>{span.model ?? 'unknown model'}</span>
      {span.tokens_input != null || span.tokens_output != null ? (
        <span>
          {span.tokens_input ?? 0}/{span.tokens_output ?? 0} tok
        </span>
      ) : null}
      {span.duration_ms != null ? <span>{span.duration_ms} ms</span> : null}
      {tools.length > 0 ? (
        <span>
          {tools.length} tool{tools.length === 1 ? '' : 's'}
          {toolErrors > 0 ? (
            <span className="text-red-400 ml-1">({toolErrors} error{toolErrors === 1 ? '' : 's'})</span>
          ) : null}
        </span>
      ) : null}
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
