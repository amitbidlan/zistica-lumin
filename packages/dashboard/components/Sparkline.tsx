'use client';

/**
 * Tiny activity sparkline. Takes per-bucket counts and renders an
 * SVG bar chart sized to fit a card footer. Bar 0 = most recent;
 * we render newest-on-the-right so the eye reads "now" at the
 * end (left → right = past → present, like an EKG).
 *
 * Heights are normalized to the max bucket in the series so
 * spikes are always visible. An all-zero series renders as a
 * thin baseline rule, not blank space.
 */
export default function Sparkline({
  buckets,
  width = 96,
  height = 20,
  bucketMinutes = 5,
}: {
  buckets: number[];
  width?: number;
  height?: number;
  bucketMinutes?: number;
}) {
  if (!buckets || buckets.length === 0) return null;

  // newest-on-the-right
  const data = [...buckets].reverse();
  const max = Math.max(1, ...data);
  const barCount = data.length;
  const gap = 1.5;
  const barWidth = (width - gap * (barCount - 1)) / barCount;

  const total = buckets.reduce((s, n) => s + n, 0);
  const minutes = barCount * bucketMinutes;
  const title =
    total === 0
      ? `No activity in the last ${minutes} min`
      : `${total} trace${total === 1 ? '' : 's'} in the last ${minutes} min`;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-label={title}
    >
      <title>{title}</title>
      {/* Baseline so the empty case isn't blank */}
      <line
        x1={0}
        y1={height - 0.5}
        x2={width}
        y2={height - 0.5}
        stroke="rgba(148, 163, 184, 0.15)"
        strokeWidth={1}
      />
      {data.map((n, i) => {
        const x = i * (barWidth + gap);
        const h = n === 0 ? 1 : Math.max(2, (n / max) * (height - 2));
        const y = height - h;
        // Most recent bar (rightmost) gets accent; older fade.
        const isLatest = i === barCount - 1;
        const fill = isLatest
          ? 'var(--accent)'
          : n > 0
          ? 'rgba(96, 165, 250, 0.55)'
          : 'rgba(148, 163, 184, 0.18)';
        return (
          <rect
            key={i}
            x={x}
            y={y}
            width={barWidth}
            height={h}
            fill={fill}
            rx={1}
          />
        );
      })}
    </svg>
  );
}
