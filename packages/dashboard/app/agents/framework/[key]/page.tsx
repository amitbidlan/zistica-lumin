import Link from 'next/link';
import AgentList from '@/components/AgentList';

const PROJECT_LABEL: Record<string, string> = {
  openclaw:  'OpenClaw',
  mastra:    'Mastra',
  voltagent: 'VoltAgent',
  default:   'Python SDK',
};

const PROJECT_ICON: Record<string, string> = {
  openclaw:  '🦞',
  mastra:    '⚡',
  voltagent: '🔌',
  default:   '🐍',
};

const PROJECT_DESC: Record<string, string> = {
  openclaw:  'Personal-AI assistants via @lumin-io/openclaw',
  mastra:    'TypeScript agents via @lumin-io/mastra',
  voltagent: 'OTel-native TS agents via @lumin-io/voltagent',
  default:   'Python SDK · LangChain / CrewAI / Anthropic / direct',
};

function projectLabel(key: string): string {
  if (key in PROJECT_LABEL) return PROJECT_LABEL[key];
  return key ? `Custom (${key})` : 'Custom';
}
function projectIcon(key: string): string {
  return PROJECT_ICON[key] ?? '🧪';
}
function projectDesc(key: string): string {
  if (key in PROJECT_DESC) return PROJECT_DESC[key];
  return key ? `Custom integration · X-Lumin-Project: ${key}` : '';
}

export function generateMetadata({ params }: { params: { key: string } }) {
  return { title: `${projectLabel(params.key)} agents` };
}

/**
 * Dedicated framework view — shows every agent in one project / SDK,
 * with no truncation. Wrapper layout mirrors /agents (max-w-7xl + page
 * header) so navigating between the two doesn't shift content. The
 * AgentList component receives lockedFramework so its internal filter
 * row swaps to a "← All frameworks" back-link.
 */
export default function FrameworkAgentsPage({
  params,
}: {
  params: { key: string };
}) {
  const key = params.key;
  const label = projectLabel(key);
  const icon = projectIcon(key);
  const desc = projectDesc(key);

  return (
    <div className="max-w-7xl mx-auto">
      <div className="mb-8">
        <Link
          href="/agents"
          className="text-[var(--muted)] text-xs hover:text-[var(--foreground)] transition-colors"
        >
          ← All agents
        </Link>
        <div className="mt-3 flex items-baseline gap-3 flex-wrap">
          <span className="text-3xl leading-none">{icon}</span>
          <h1 className="text-2xl font-semibold tracking-tight">
            {label} agents
          </h1>
        </div>
        {desc ? (
          <p className="text-[var(--muted)] text-sm mt-1.5 max-w-2xl">
            {desc}
          </p>
        ) : null}
      </div>
      <AgentList lockedFramework={key} />
    </div>
  );
}
