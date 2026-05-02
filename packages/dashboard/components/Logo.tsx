/** Synaptic logo: a 3-node trace tree (parent + 2 children).
 *  Thematically: the product visualizes trace trees, so the mark
 *  literally is a trace tree. Colors match the SpanRow type badges
 *  (blue root, violet for llm, cyan for tool).
 */
export default function Logo({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="Synaptic"
      role="img"
    >
      <path
        d="M7 12 L17 6.5"
        stroke="#475569"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <path
        d="M7 12 L17 17.5"
        stroke="#475569"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <circle cx="6" cy="12" r="2.6" fill="#60a5fa" />
      <circle cx="18" cy="6.5" r="2.1" fill="#a78bfa" />
      <circle cx="18" cy="17.5" r="2.1" fill="#22d3ee" />
    </svg>
  );
}
