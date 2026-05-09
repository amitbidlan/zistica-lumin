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

  // Lumin firewall: a *decision* on a trace doesn't always mean Lumin
  // replaced the reply. Three cases the chat needs to disambiguate:
  //
  //   1. Lumin replaced the reply (takeover) — trace.output differs
  //      from the LLM span's output, OR the LLM span has no output
  //      at all (LLM was bypassed entirely). Render Lumin as a chat
  //      participant with its own reply bubble.
  //
  //   2. Lumin only observed / flagged — trace.output equals the LLM
  //      span's output. The LLM's reply was delivered unchanged; Lumin
  //      logged a decision but never spoke. Render a slim annotation
  //      strip, NOT a fake reply bubble. (Showing the LLM's text as
  //      if Lumin said it would be a lie — and operators noticed.)
  //
  //   3. No decision at all — nothing to render.
  const llmText = (headline.output ?? '').trim();
  const luminText = (trace.output ?? '').trim();
  const luminReplaced =
    !!trace.firewall_decision_count &&
    !!trace.firewall_top_verb &&
    !!luminText &&
    luminText !== llmText;
  const luminObservedOnly =
    !!trace.firewall_decision_count && !luminReplaced;

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
        {luminReplaced ? (
          <LuminBubble
            text={trace.output ?? ''}
            verb={trace.firewall_top_verb ?? null}
            policy={trace.firewall_top_policy ?? null}
            traceId={trace.id}
            blocked={trace.firewall_blocked ?? false}
          />
        ) : null}
        {luminObservedOnly ? (
          <LuminObservation
            verb={trace.firewall_top_verb ?? null}
            policy={trace.firewall_top_policy ?? null}
            traceId={trace.id}
            blocked={trace.firewall_blocked ?? false}
            decisionCount={trace.firewall_decision_count ?? 0}
          />
        ) : null}
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


/**
 * Lumin's reply, rendered as a chat bubble alongside the LLM's
 * reply. Visually distinct (rose/red panel, shield avatar, "Lumin
 * Firewall" header, decision metadata) so operators see at a glance
 * which messages came from policy enforcement vs. the model itself.
 *
 * The verb drives palette + verb-specific copy:
 *   block            → rose, "replaced the reply"
 *   rewrite          → cyan, "rewrote the reply"
 *   require_approval → amber, "paused for approval"
 *   (other)          → slate, "observed"
 */
function LuminBubble({
  text,
  verb,
  policy,
  traceId,
  blocked,
}: {
  text: string;
  verb: 'block' | 'require_approval' | 'rewrite' | null;
  policy: string | null;
  traceId: string;
  blocked: boolean;
}) {
  const palette = (() => {
    if (verb === 'block') {
      return blocked
        ? {
            panel:
              'bg-rose-500/[0.12] border-rose-500/60 border-l-4 border-l-rose-500 shadow-[0_0_24px_-12px_rgba(244,63,94,0.6)]',
            icon: 'text-rose-400 bg-rose-500/15 border-rose-500/40',
            label: 'text-rose-200',
            policy: 'text-rose-300/70',
            body: 'text-rose-50',
            footer: 'text-rose-300/70',
            link: 'text-rose-200 underline decoration-rose-400/60 hover:decoration-rose-200',
          }
        : {
            panel: 'bg-rose-500/[0.06] border-rose-500/30 border-l-4 border-l-rose-500/60',
            icon: 'text-rose-400 bg-rose-500/10 border-rose-500/30',
            label: 'text-rose-300',
            policy: 'text-rose-400/60',
            body: 'text-rose-100/90',
            footer: 'text-rose-400/60',
            link: 'text-rose-300 underline hover:no-underline',
          };
    }
    if (verb === 'rewrite') {
      return {
        panel: 'bg-cyan-500/[0.10] border-cyan-500/50 border-l-4 border-l-cyan-500',
        icon: 'text-cyan-400 bg-cyan-500/15 border-cyan-500/40',
        label: 'text-cyan-200',
        policy: 'text-cyan-300/70',
        body: 'text-cyan-50',
        footer: 'text-cyan-300/70',
        link: 'text-cyan-200 underline decoration-cyan-400/60 hover:decoration-cyan-200',
      };
    }
    if (verb === 'require_approval') {
      return {
        panel: 'bg-amber-500/[0.10] border-amber-500/50 border-l-4 border-l-amber-500',
        icon: 'text-amber-400 bg-amber-500/15 border-amber-500/40',
        label: 'text-amber-200',
        policy: 'text-amber-300/70',
        body: 'text-amber-50',
        footer: 'text-amber-300/70',
        link: 'text-amber-200 underline decoration-amber-400/60 hover:decoration-amber-200',
      };
    }
    return {
      panel: 'bg-slate-500/[0.06] border-slate-500/40',
      icon: 'text-slate-400 bg-slate-500/15 border-slate-500/40',
      label: 'text-slate-200',
      policy: 'text-slate-400',
      body: 'text-slate-100',
      footer: 'text-slate-400',
      link: 'text-slate-200 underline hover:no-underline',
    };
  })();

  const verbLabel =
    verb === 'block'
      ? blocked
        ? 'replaced the reply (LLM bypassed or overridden)'
        : 'would have replaced the reply (shadow mode)'
      : verb === 'rewrite'
      ? 'rewrote the reply (sensitive content redacted)'
      : verb === 'require_approval'
      ? 'paused this turn pending operator approval'
      : 'observed this turn';

  return (
    <div className="flex justify-start">
      <div className="flex items-start gap-2 max-w-[80%]">
        {/* Shield avatar — same circular slot as the user avatar in
            the sender header so Lumin reads as a participant, not an
            annotation. */}
        <div
          className={`h-8 w-8 rounded-full border flex items-center justify-center shrink-0 ${palette.icon}`}
          title="Lumin Firewall"
          aria-label="Lumin Firewall"
        >
          <ShieldGlyph />
        </div>
        <div
          className={`flex-1 rounded-2xl rounded-bl-sm border px-4 py-2.5 text-sm whitespace-pre-wrap break-words ${palette.panel}`}
        >
          <div className="flex items-center gap-1.5 mb-1 flex-wrap">
            <span
              className={`text-[10px] uppercase tracking-wider font-semibold ${palette.label}`}
            >
              Lumin Firewall
            </span>
            {policy ? (
              <span
                className={`text-[10px] font-mono ${palette.policy}`}
                title={`policy: ${policy}`}
              >
                · {policy}
              </span>
            ) : null}
          </div>
          <div className={palette.body}>
            {text || <span className="italic opacity-70">(no reply)</span>}
          </div>
          <div className={`mt-2 text-[10px] ${palette.footer}`}>
            {verbLabel} ·{' '}
            <a
              href={`/decisions?trace_id=${encodeURIComponent(traceId)}`}
              className={palette.link}
            >
              View decision →
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}


/**
 * Slim annotation strip rendered when Lumin recorded a decision but
 * didn't actually replace the LLM's reply. Distinguishable from the
 * LuminBubble at a glance: no avatar, no quoted text, just a
 * timeline-style note. Honest about what happened — "decision
 * recorded, reply unchanged" — so operators don't misread it as a
 * Lumin reply.
 */
function LuminObservation({
  verb,
  policy,
  traceId,
  blocked,
  decisionCount,
}: {
  verb: 'block' | 'require_approval' | 'rewrite' | null;
  policy: string | null;
  traceId: string;
  blocked: boolean;
  decisionCount: number;
}) {
  const tone =
    verb === 'block'
      ? blocked
        ? 'border-rose-500/40 bg-rose-500/[0.04] text-rose-300'
        : 'border-rose-500/30 bg-rose-500/[0.03] text-rose-300/80'
      : verb === 'require_approval'
      ? 'border-amber-500/40 bg-amber-500/[0.04] text-amber-300'
      : verb === 'rewrite'
      ? 'border-cyan-500/40 bg-cyan-500/[0.04] text-cyan-300'
      : 'border-slate-500/30 bg-slate-500/[0.03] text-slate-300';

  const verbLabel = (() => {
    if (verb === 'block') {
      return blocked
        ? 'recorded a BLOCK decision in enforce mode'
        : 'would have blocked (shadow mode)';
    }
    if (verb === 'rewrite') return 'flagged for rewrite';
    if (verb === 'require_approval') return 'flagged for approval';
    return 'observed this turn';
  })();

  return (
    <div
      className={`flex items-start gap-2 rounded-md border border-dashed ${tone} px-3 py-2 text-[11px] font-mono`}
      role="note"
    >
      <span className="mt-0.5 shrink-0">
        <ShieldGlyph />
      </span>
      <div className="flex-1 leading-relaxed">
        <span className="font-semibold uppercase tracking-wider mr-1.5">
          Lumin Firewall
        </span>
        <span>{verbLabel}</span>
        {policy ? (
          <>
            <span className="opacity-50"> · </span>
            <span title={`policy: ${policy}`}>{policy}</span>
          </>
        ) : null}
        <span className="opacity-50"> · </span>
        <span>
          {decisionCount} decision{decisionCount === 1 ? '' : 's'} —{' '}
          <span className="opacity-80">reply was not modified</span>
        </span>
        <span className="opacity-50"> · </span>
        <a
          href={`/decisions?trace_id=${encodeURIComponent(traceId)}`}
          className="underline decoration-dotted hover:no-underline"
        >
          View decision →
        </a>
      </div>
    </div>
  );
}


function ShieldGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      width={14}
      height={14}
      fill="currentColor"
      aria-hidden
    >
      <path d="M12 2L4 5v6c0 5 3.5 9.5 8 11 4.5-1.5 8-6 8-11V5l-8-3zm0 2.18L18 6.5V11c0 4-2.7 7.7-6 9-3.3-1.3-6-5-6-9V6.5l6-2.32z" />
    </svg>
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
