'use client';

import { Span } from '@/lib/api';
import SpanRow from './SpanRow';

type SpanNode = Span & { children: SpanNode[] };

function buildTree(spans: Span[]): SpanNode[] {
  const byId = new Map<string, SpanNode>();
  for (const s of spans) {
    byId.set(s.id, { ...s, children: [] });
  }

  const roots: SpanNode[] = [];
  byId.forEach((node) => {
    const parentId = node.parent_span_id;
    if (parentId && byId.has(parentId)) {
      byId.get(parentId)!.children.push(node);
    } else {
      roots.push(node);
    }
  });

  const sortByStart = (a: SpanNode, b: SpanNode) =>
    (a.started_at ?? '').localeCompare(b.started_at ?? '');
  const sortRecursive = (n: SpanNode) => {
    n.children.sort(sortByStart);
    n.children.forEach(sortRecursive);
  };
  roots.sort(sortByStart);
  roots.forEach(sortRecursive);

  return roots;
}

export default function SpanTimeline({ spans }: { spans: Span[] }) {
  const tree = buildTree(spans);
  return (
    <div className="border border-[var(--border)] rounded divide-y divide-[var(--border)]">
      {tree.map((node) => (
        <NodeRows key={node.id} node={node} depth={0} />
      ))}
    </div>
  );
}

function NodeRows({ node, depth }: { node: SpanNode; depth: number }) {
  return (
    <>
      <SpanRow span={node} depth={depth} />
      {node.children.map((child) => (
        <NodeRows key={child.id} node={child} depth={depth + 1} />
      ))}
    </>
  );
}
