import type { Trace } from '@/lib/api';

/**
 * Compact badge surfaced on every trace card / detail page when
 * Lumin's firewall took action against the trace. The visual
 * intent: a red shield + policy name that's instantly readable
 * from a list of 50+ traces, so operators can scan and spot
 * "which conversations did Lumin step into?"
 *
 * Three render variants based on the decision verb that fired:
 *
 *   block            → solid rose/red — hard refusal, LLM may have
 *                      been bypassed entirely (input-side) or had
 *                      its tool call cancelled
 *   require_approval → amber — operator-pending; the LLM was paused
 *   rewrite          → cyan — output redacted (PII / secrets)
 *
 * The badge is omitted entirely when ``firewall_decision_count``
 * is 0 — keeps the existing UI uncluttered for clean traces.
 */
export function LuminFirewallBadge({
  trace,
  size = 'sm',
}: {
  trace: Pick<Trace, 'firewall_blocked' | 'firewall_decision_count' | 'firewall_top_policy' | 'firewall_top_verb'>;
  size?: 'sm' | 'lg';
}) {
  const count = trace.firewall_decision_count ?? 0;
  if (count === 0) return null;

  const verb = trace.firewall_top_verb ?? null;
  const policy = trace.firewall_top_policy ?? null;
  const blocked = trace.firewall_blocked ?? false;

  // Compute color class based on verb. Solid for hard blocks
  // (blocked === true), softer for shadow / observation rows
  // where the firewall recorded but didn't actually intervene.
  const palette = (() => {
    if (verb === 'block') {
      return blocked
        ? 'bg-rose-500/20 text-rose-200 border-rose-500/60'
        : 'bg-rose-500/10 text-rose-300 border-rose-500/30';
    }
    if (verb === 'require_approval') {
      return 'bg-amber-500/15 text-amber-200 border-amber-500/40';
    }
    if (verb === 'rewrite') {
      return 'bg-cyan-500/15 text-cyan-200 border-cyan-500/40';
    }
    // No top-verb but decision count > 0 → flag/observation only
    return 'bg-slate-500/10 text-slate-300 border-slate-500/30';
  })();

  const verbLabel = (() => {
    if (verb === 'block') return blocked ? 'Lumin blocked' : 'Lumin would block';
    if (verb === 'require_approval') return 'Lumin: approval required';
    if (verb === 'rewrite') return 'Lumin redacted';
    return 'Lumin observed';
  })();

  const sizeCls =
    size === 'lg'
      ? 'px-3 py-1.5 text-sm gap-2'
      : 'px-2 py-0.5 text-[10px] gap-1';

  return (
    <span
      className={`inline-flex items-center rounded border font-medium font-mono ${palette} ${sizeCls}`}
      title={
        policy
          ? `Lumin firewall: ${policy} (${verb ?? 'observed'}) — ${count} decision${count === 1 ? '' : 's'} on this trace`
          : `Lumin firewall: ${count} decision${count === 1 ? '' : 's'} on this trace`
      }
    >
      <ShieldIcon size={size} />
      <span>{verbLabel}</span>
      {policy ? (
        <span className="opacity-70 truncate max-w-[14ch]">· {policy}</span>
      ) : null}
    </span>
  );
}


function ShieldIcon({ size }: { size: 'sm' | 'lg' }) {
  const px = size === 'lg' ? 14 : 10;
  return (
    <svg
      viewBox="0 0 24 24"
      width={px}
      height={px}
      fill="currentColor"
      aria-hidden
      className="shrink-0"
    >
      <path d="M12 2L4 5v6c0 5 3.5 9.5 8 11 4.5-1.5 8-6 8-11V5l-8-3zm0 2.18L18 6.5V11c0 4-2.7 7.7-6 9-3.3-1.3-6-5-6-9V6.5l6-2.32z" />
    </svg>
  );
}


/**
 * Banner variant — used at the top of /traces/{id} when the
 * firewall acted. Larger, more contextual: includes the policy
 * link, the verb, and a CTA into /decisions filtered to this
 * trace_id.
 */
export function LuminFirewallBanner({ trace }: { trace: Trace }) {
  if (!trace.firewall_decision_count) return null;

  const verb = trace.firewall_top_verb;
  const policy = trace.firewall_top_policy;
  const isBlock = verb === 'block' && trace.firewall_blocked;

  // Visual hierarchy: a hard enforce-mode block needs to *look*
  // like a stop sign — saturated background, vivid border, red
  // heading, and a left-edge accent bar so it's identifiable from
  // 10 feet away. Other verbs stay calmer (operator hasn't been
  // overridden, just nudged).
  const palette = isBlock
    ? {
        panel: 'border-rose-500/70 bg-rose-500/[0.14] border-l-4 border-l-rose-500 shadow-[0_0_24px_-12px_rgba(244,63,94,0.6)]',
        icon: 'text-rose-400',
        heading: 'text-rose-200',
        body: 'text-rose-300/70',
        link: 'text-rose-200 underline decoration-rose-400/60 hover:decoration-rose-200',
      }
    : verb === 'require_approval'
    ? {
        panel: 'border-amber-500/60 bg-amber-500/[0.10] border-l-4 border-l-amber-500',
        icon: 'text-amber-400',
        heading: 'text-amber-200',
        body: 'text-amber-300/70',
        link: 'text-amber-200 underline decoration-amber-400/60 hover:decoration-amber-200',
      }
    : verb === 'rewrite'
    ? {
        panel: 'border-cyan-500/60 bg-cyan-500/[0.10] border-l-4 border-l-cyan-500',
        icon: 'text-cyan-400',
        heading: 'text-cyan-200',
        body: 'text-cyan-300/70',
        link: 'text-cyan-200 underline decoration-cyan-400/60 hover:decoration-cyan-200',
      }
    : {
        panel: 'border-slate-500/40 bg-slate-500/[0.04]',
        icon: 'text-slate-400',
        heading: 'text-slate-200',
        body: 'text-[var(--muted)]',
        link: 'underline hover:no-underline',
      };

  const heading = isBlock
    ? 'Lumin firewall blocked this trace'
    : verb === 'require_approval'
    ? 'Lumin firewall paused this trace for approval'
    : verb === 'rewrite'
    ? 'Lumin firewall rewrote this trace'
    : 'Lumin firewall observed this trace';

  return (
    <div className={`card p-4 border ${palette.panel}`}>
      <div className="flex items-start gap-3">
        <span className={palette.icon}>
          <ShieldIcon size="lg" />
        </span>
        <div className="flex-1">
          <div className={`font-semibold text-sm ${palette.heading}`}>{heading}</div>
          <div className={`text-xs mt-1 ${palette.body}`}>
            {policy ? (
              <>
                Top policy: <span className="font-mono">{policy}</span>
                {' · '}
              </>
            ) : null}
            {trace.firewall_decision_count} decision
            {trace.firewall_decision_count === 1 ? '' : 's'} recorded.{' '}
            <a
              href={`/decisions?trace_id=${encodeURIComponent(trace.id)}`}
              className={palette.link}
            >
              View all →
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
