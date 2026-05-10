/**
 * Path D — per-sender workspace sandboxing.
 *
 * Cross-session leaks happen at the STORAGE layer, not the
 * outbound layer. If the agent writes user A's secret to a
 * shared MEMORY.md and later user B asks the agent, the LLM
 * reads the same file and recites the secret — no firewall on
 * the *output* side can prevent this without playing whack-a-
 * mole with hooks, channels, and SDK versions.
 *
 * The architecturally correct fix: sandbox file-system tool
 * calls per-sender. User A's writes go to
 * ``{workspaceDir}/_lumin/by-sender/{safe-A}/...`` and user B's
 * reads resolve relative to ``{workspaceDir}/_lumin/by-sender/{safe-B}/``.
 * The bot literally has no path to user A's data while
 * serving user B — the leak is impossible by construction.
 *
 * Why plugin-side (not server-side rule):
 *   - Path resolution is channel-agnostic and protocol-agnostic.
 *   - ``before_tool_call`` IS awaited and CAN return rewritten
 *     params. We modify the path in place before the tool runs.
 *   - No server round-trip per fs call → zero added latency.
 *
 * What this does NOT cover (yet — separate rules):
 *   - Tools that read from outside the workspace dir (e.g.
 *     web_fetch, http_get). Those leaks are different and need
 *     their own egress policy (see TENANT_ISOLATION_SPEC.md L1.5).
 *   - Subagent / nested-session sharing. Subagents inherit the
 *     parent's workspaceDir; sandboxing applies recursively.
 *   - Race between concurrent senders writing to the SAME
 *     sandbox directory — handled by per-call mkdirSync (atomic).
 */

import * as path from "path";
import * as fs from "fs";


/**
 * Read-only fs tools. Shared-path matches are allowed for these
 * (the operator opted them in via ``shared_paths`` config).
 */
export const FS_READONLY_TOOL_NAMES = new Set([
  "read",
  "search",
  "ls",
  "grep",
  "find",
  "cat",
  "head",
  "tail",
]);

/**
 * Write-capable fs tools. Targeting a shared path with one of
 * these is blocked — shared paths are read-only by default per
 * TENANT_ISOLATION_SPEC §2.1. A future ``shared_writable`` flag
 * can opt specific paths into writability.
 */
export const FS_WRITE_TOOL_NAMES = new Set([
  "edit",
  "write",
]);

/**
 * Every fs tool name we sandbox. Sourced from OpenClaw's
 * pi-coding-agent (read/edit/write/search/ls) plus the standard
 * shell-style readers (grep/find/cat/head/tail) operators
 * commonly expose to agents. Exact-match check — an attacker
 * can't bypass by aliasing because Lumin also blocks
 * unrecognised fs tools via a separate ``unknown_fs_tool`` rule.
 */
export const FS_TOOL_NAMES = new Set([
  ...FS_READONLY_TOOL_NAMES,
  ...FS_WRITE_TOOL_NAMES,
]);


/**
 * The path-parameter field name for each tool. Most use
 * ``path``; ``search`` and ``grep`` carry the search root in
 * ``path`` per OpenClaw's tool schema.
 */
const PATH_FIELDS: Record<string, string[]> = {
  read: ["path"],
  edit: ["path"],
  write: ["path"],
  search: ["path"],
  ls: ["path"],
  grep: ["path"],
  find: ["path"],
  cat: ["path"],
  head: ["path"],
  tail: ["path"],
};


/**
 * Make a senderId safe for use as a directory name. Replaces
 * EVERY char outside ``[A-Za-z0-9_-]`` with ``-`` — including
 * dots, so an attacker can't sneak a ``..`` parent-traversal
 * sequence into the sender component. Real-world senderIds use
 * colons (``slack:U09…``) or are pure digits (Telegram chat
 * IDs); none of them legitimately contain ``..``. Empty /
 * undefined senders fall back to ``_anonymous`` so the firewall
 * still isolates anonymous traffic from authenticated users.
 */
export function sanitizeSenderForFs(senderId: string | undefined): string {
  if (!senderId) return "_anonymous";
  const safe = senderId.replace(/[^A-Za-z0-9_-]/g, "-");
  return safe.length > 0 ? safe : "_anonymous";
}


/**
 * Compute the sandbox root for a given workspace + sender.
 * Always under ``{workspaceDir}/_lumin/by-sender/{safe}/`` —
 * the ``_lumin`` prefix is reserved so operators can grep / git-
 * ignore Lumin-managed state without colliding with the user's
 * own files.
 */
export function senderSandboxRoot(
  workspaceDir: string,
  senderId: string | undefined,
): string {
  return path.join(
    workspaceDir,
    "_lumin",
    "by-sender",
    sanitizeSenderForFs(senderId),
  );
}


/**
 * Decide what to do with a requested path under the per-sender
 * sandbox model:
 *
 *   - ``inside-sandbox`` — already inside the right sandbox, allow as-is.
 *   - ``rewrite`` — would touch user-data outside this sender's
 *     sandbox; rewrite to the sandbox-equivalent path.
 *   - ``outside-workspace`` — path is outside the workspace dir
 *     entirely (e.g. ``/etc/passwd``, ``/tmp/foo``). The
 *     workspace-isolation rule doesn't manage these; let other
 *     firewall rules decide.
 *
 * Returns the resolved absolute path along with the verdict.
 */
export type SandboxVerdict =
  | { kind: "inside-sandbox"; resolved: string }
  | { kind: "rewrite"; original: string; rewritten: string }
  | { kind: "outside-workspace"; resolved: string };

export function classifyPath(
  requestedPath: string,
  workspaceDir: string,
  senderId: string | undefined,
): SandboxVerdict {
  const sandbox = senderSandboxRoot(workspaceDir, senderId);
  const wasRelative = !path.isAbsolute(requestedPath);
  // Resolve relative paths against the sandbox first (so the
  // LLM's "MEMORY.md" lands inside its own sender's dir, not
  // the global workspace root). Absolute paths pass through
  // path.resolve unchanged.
  const resolved = path.resolve(
    wasRelative ? sandbox : "/",
    requestedPath,
  );

  // Normalize prefixes for prefix-comparison. Append a trailing
  // separator to ``sandbox`` and ``workspaceDir`` so e.g.
  // ``/ws_other/`` doesn't match ``/ws/`` as a prefix.
  const ws = path.resolve(workspaceDir);
  const wsWithSep = ws.endsWith(path.sep) ? ws : ws + path.sep;
  const sb = path.resolve(sandbox);
  const sbWithSep = sb.endsWith(path.sep) ? sb : sb + path.sep;

  const insideSandbox = resolved === sb || resolved.startsWith(sbWithSep);
  if (insideSandbox && !wasRelative) {
    // Already absolute AND already in our sandbox — pass through.
    return { kind: "inside-sandbox", resolved };
  }
  if (insideSandbox && wasRelative) {
    // Relative path that would resolve into our sandbox by
    // path-resolution rules. We still need to REWRITE so the
    // tool sees the absolute sandbox path — otherwise its own
    // path resolution (relative to workspace root, not sandbox)
    // would land it in the GLOBAL workspace, not our sandbox.
    // This is the case that caused the live-test leak: the LLM
    // emitted ``path: "MEMORY.md"``, which classifyPath would
    // have called inside-sandbox, but the tool resolved it
    // against ``workspaceDir`` (the wrong base).
    return { kind: "rewrite", original: requestedPath, rewritten: resolved };
  }
  if (resolved === ws || resolved.startsWith(wsWithSep)) {
    // Inside workspace but outside sandbox — rewrite to the
    // sandbox-equivalent path. Strip the ws prefix and append
    // the remainder under sb.
    const remainder = resolved.slice(ws.length).replace(/^[/\\]+/, "");
    const rewritten = path.join(sb, remainder);
    return { kind: "rewrite", original: resolved, rewritten };
  }
  return { kind: "outside-workspace", resolved };
}


/**
 * Convert a glob pattern to an anchored regex. Supports ``*``
 * (any chars except ``/``) and ``**`` (any chars including
 * ``/``). Other regex metacharacters are escaped.
 *
 * MVP scope: no brace expansion, no character classes, no
 * negation. The shared_paths config is operator-controlled so
 * we don't need to defend against pathological patterns — but
 * we do need to be deterministic.
 */
function globToRegex(pattern: string): RegExp {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
  // Convert ``**`` first via a placeholder, then ``*``, so the
  // single-star pass doesn't eat the second star of a ``**``.
  const NUL = " ";
  const regexStr = escaped
    .replace(/\*\*/g, `${NUL}DSTAR${NUL}`)
    .replace(/\*/g, "[^/]*")
    .replace(new RegExp(`${NUL}DSTAR${NUL}`, "g"), ".*");
  return new RegExp(`^${regexStr}$`);
}


/**
 * Test whether a requested path matches any of the operator's
 * declared shared_paths entries. Patterns are interpreted
 * relative to ``workspaceDir`` if relative, or as-absolute
 * otherwise. Examples:
 *
 *   - ``"AGENTS.md"`` matches ``${ws}/AGENTS.md`` exactly.
 *   - ``"docs/**"`` matches anything under ``${ws}/docs/``.
 *   - ``"/etc/openclaw/*"`` matches one level under
 *     ``/etc/openclaw/`` (absolute).
 *
 * Returns the matched pattern + the resolved absolute path of
 * the request (for the caller to use as the rewritten param).
 */
export function matchesSharedPath(
  requestedPath: string,
  sharedPatterns: string[],
  workspaceDir: string,
): { matched: string; resolved: string } | undefined {
  if (!sharedPatterns || sharedPatterns.length === 0) return undefined;
  const resolvedRequest = path.isAbsolute(requestedPath)
    ? path.resolve(requestedPath)
    : path.resolve(workspaceDir, requestedPath);
  for (const pattern of sharedPatterns) {
    if (typeof pattern !== "string" || pattern.length === 0) continue;
    const absPattern = path.isAbsolute(pattern)
      ? pattern
      : path.resolve(workspaceDir, pattern);
    let rx: RegExp;
    try {
      rx = globToRegex(absPattern);
    } catch {
      continue;
    }
    if (rx.test(resolvedRequest)) {
      return { matched: pattern, resolved: resolvedRequest };
    }
  }
  return undefined;
}


/**
 * Outcome of applying tenant-isolation policy to a tool call.
 *
 *   - undefined → no change (not an fs tool, no path field, or
 *     the path is already inside the right sandbox).
 *   - ``{ kind: "rewrite" }`` → params were rewritten to the
 *     per-sender sandbox; caller should pass these to the tool.
 *   - ``{ kind: "shared" }`` → path matched an operator-declared
 *     shared_paths entry. Params still need to be replaced with
 *     the resolved absolute path so the tool reads the global
 *     file (not the per-sender copy that would otherwise apply).
 *   - ``{ kind: "block" }`` → fail-closed. Caller should refuse
 *     the tool call and surface ``reason`` to the audit row.
 */
export type SandboxOutcome =
  | {
      kind: "rewrite";
      params: Record<string, unknown>;
      rewrittenPaths: Array<{ original: string; rewritten: string }>;
    }
  | {
      kind: "shared";
      params: Record<string, unknown>;
      sharedMatches: Array<{ pattern: string; resolved: string }>;
    }
  | {
      kind: "block";
      reason: string;
    };


/**
 * Apply sandboxing to a tool call's params.
 *
 * @param sharedPaths - Operator-declared paths that bypass the
 *   per-sender sandbox (read-only). Glob patterns relative to
 *   ``workspaceDir`` or absolute. Defaults to none.
 * @param failClosed - When true, missing ``workspaceDir`` for an
 *   fs tool returns a ``block`` outcome instead of silently
 *   passing through. Production should set this to true; the
 *   default (false) preserves backward-compatible fail-warn
 *   behaviour.
 *
 * Side effect: ensures the destination directory exists on disk
 * for rewrite outcomes (mkdirSync recursive — idempotent and
 * concurrency-safe).
 */
export function sandboxToolParams(args: {
  toolName: string;
  params: Record<string, unknown>;
  senderId: string | undefined;
  workspaceDir: string | undefined;
  sharedPaths?: string[];
  failClosed?: boolean;
}): SandboxOutcome | undefined {
  const fields = PATH_FIELDS[args.toolName];
  if (!fields || fields.length === 0) return undefined;

  if (!args.workspaceDir) {
    // Fail-closed: an fs tool reached us without a workspaceDir
    // in hook context. The sandbox can't bind the call to a
    // sender, so the only safe move is to refuse it. Production
    // deployments opt in via ``failClosed: true``; the default
    // preserves the old fail-warn (silent passthrough) behaviour
    // so existing operators don't see a regression.
    if (args.failClosed) {
      return { kind: "block", reason: "missing_workspace_dir" };
    }
    return undefined;
  }

  const newParams = { ...args.params };
  const rewrittenPaths: Array<{ original: string; rewritten: string }> = [];
  const sharedMatches: Array<{ pattern: string; resolved: string }> = [];
  const sharedPaths = args.sharedPaths ?? [];
  const isWriteTool = FS_WRITE_TOOL_NAMES.has(args.toolName);

  for (const field of fields) {
    const v = args.params[field];
    if (typeof v !== "string" || v.length === 0) continue;

    // Step 1: shared_paths takes precedence — operator
    // explicitly opted these in. Read-only access only; writes
    // are blocked.
    const shared = matchesSharedPath(v, sharedPaths, args.workspaceDir);
    if (shared) {
      if (isWriteTool) {
        return {
          kind: "block",
          reason: `write_to_shared_path:${shared.matched}`,
        };
      }
      newParams[field] = shared.resolved;
      sharedMatches.push({ pattern: shared.matched, resolved: shared.resolved });
      continue;
    }

    // Step 2: per-sender sandbox.
    const verdict = classifyPath(v, args.workspaceDir, args.senderId);
    if (verdict.kind === "rewrite") {
      newParams[field] = verdict.rewritten;
      rewrittenPaths.push({
        original: verdict.original,
        rewritten: verdict.rewritten,
      });
      // Ensure the parent directory exists before the tool tries
      // to read/write it. ``recursive: true`` makes this safe
      // across concurrent calls (mkdir is atomic with that flag).
      try {
        const dir = path.dirname(verdict.rewritten);
        fs.mkdirSync(dir, { recursive: true });
      } catch {
        // Best-effort. If mkdir fails the tool will fail with a
        // clear error and the operator can fix permissions.
      }
    }
  }

  // Shared and rewrite are mutually exclusive in the single-
  // path-field tools we currently support. If a future
  // multi-field tool produced both, prefer ``shared`` (operator
  // explicitly opted in) and leave any per-sender rewrites
  // applied to the same params object.
  if (sharedMatches.length > 0) {
    return { kind: "shared", params: newParams, sharedMatches };
  }
  if (rewrittenPaths.length > 0) {
    return { kind: "rewrite", params: newParams, rewrittenPaths };
  }
  return undefined;
}
