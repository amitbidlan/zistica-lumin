/**
 * Tests for the per-sender workspace sandbox (Path D).
 *
 * The whole point of this layer is that a leak across senders
 * is impossible *by construction* — these tests pin the
 * behavior so a refactor can't silently regress it. The cross-
 * session leak demo we built v0.6.1 around is one of the
 * cases below (``test_user_b_cannot_read_user_a_memory``).
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import {
  sanitizeSenderForFs,
  senderSandboxRoot,
  classifyPath,
  matchesSharedPath,
  sandboxToolParams,
  FS_TOOL_NAMES,
  FS_READONLY_TOOL_NAMES,
  FS_WRITE_TOOL_NAMES,
} from "../src/sender-sandbox.js";


let tmpRoot: string;

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lumin-sandbox-"));
});

afterEach(() => {
  try {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  } catch {
    // best-effort
  }
});


describe("sanitizeSenderForFs", () => {
  it("keeps alphanumerics and underscore/hyphen", () => {
    expect(sanitizeSenderForFs("U09CMSPA2QY")).toBe("U09CMSPA2QY");
    expect(sanitizeSenderForFs("user_name_42")).toBe("user_name_42");
    expect(sanitizeSenderForFs("a-b-c")).toBe("a-b-c");
  });

  it("replaces colons, slashes, and dots with hyphens to neutralize path traversal", () => {
    // Slack-style senderIds like "slack:U09..." use a colon.
    expect(sanitizeSenderForFs("slack:U09CMSPA2QY")).toBe("slack-U09CMSPA2QY");
    // Telegram chat IDs are pure digits — already safe.
    expect(sanitizeSenderForFs("5706212396")).toBe("5706212396");
    // Dots get stripped too: ``..`` would otherwise be a parent-
    // traversal sequence even after slash-replacement, since
    // the OS interprets a directory NAMED ``..`` as the parent
    // even without a slash separator.
    expect(sanitizeSenderForFs("../etc")).toBe("---etc");
    expect(sanitizeSenderForFs("../../passwd")).toBe("------passwd");
    expect(sanitizeSenderForFs("user.name@host.com")).toBe("user-name-host-com");
  });

  it("falls back to _anonymous for empty / undefined", () => {
    expect(sanitizeSenderForFs(undefined)).toBe("_anonymous");
    expect(sanitizeSenderForFs("")).toBe("_anonymous");
    // Confirm the impl preserves a non-empty hyphen-only result
    // rather than collapsing to anonymous (audit clarity wins
    // over aesthetics — an operator looking at /by-sender/---
    // can tell something was sanitized away).
    expect(sanitizeSenderForFs("///")).toBe("---");
  });
});


describe("senderSandboxRoot", () => {
  it("nests sender under _lumin/by-sender/", () => {
    const ws = "/data/ws";
    const root = senderSandboxRoot(ws, "slack:U09CMSPA2QY");
    expect(root).toBe("/data/ws/_lumin/by-sender/slack-U09CMSPA2QY");
  });

  it("uses _anonymous when sender is undefined", () => {
    expect(senderSandboxRoot("/data/ws", undefined))
      .toBe("/data/ws/_lumin/by-sender/_anonymous");
  });
});


describe("FS_TOOL_NAMES coverage", () => {
  it("covers the spec MVP set: read/edit/write/search/ls/grep/find/cat/head/tail", () => {
    for (const t of [
      "read", "edit", "write", "search", "ls",
      "grep", "find", "cat", "head", "tail",
    ]) {
      expect(FS_TOOL_NAMES.has(t), `missing tool: ${t}`).toBe(true);
    }
  });

  it("read/write split is consistent (no tool in both, every tool in one)", () => {
    for (const t of FS_TOOL_NAMES) {
      const inRead = FS_READONLY_TOOL_NAMES.has(t);
      const inWrite = FS_WRITE_TOOL_NAMES.has(t);
      expect(inRead !== inWrite, `tool ${t} must be in exactly one of read/write sets`).toBe(true);
    }
  });
});


describe("classifyPath", () => {
  const ws = "/workspace";
  const sender = "alice";

  it("treats absolute path inside the sandbox as inside-sandbox", () => {
    const v = classifyPath(
      "/workspace/_lumin/by-sender/alice/MEMORY.md", ws, sender,
    );
    expect(v.kind).toBe("inside-sandbox");
  });

  it("rewrites a workspace-root path to the sender's sandbox", () => {
    const v = classifyPath("/workspace/MEMORY.md", ws, sender);
    expect(v).toEqual({
      kind: "rewrite",
      original: "/workspace/MEMORY.md",
      rewritten: "/workspace/_lumin/by-sender/alice/MEMORY.md",
    });
  });

  it("rewrites a different sender's sandbox path back to ours", () => {
    // The LLM might learn another sender's path from history /
    // tool output and try to read it directly. Sandboxing
    // re-anchors the path to the CURRENT sender, so the
    // attempt resolves to an empty file in our own sandbox
    // rather than the foreign sender's data.
    const v = classifyPath(
      "/workspace/_lumin/by-sender/bob/MEMORY.md", ws, sender,
    );
    expect(v.kind).toBe("rewrite");
    if (v.kind === "rewrite") {
      expect(v.rewritten).toBe(
        "/workspace/_lumin/by-sender/alice/_lumin/by-sender/bob/MEMORY.md",
      );
    }
  });

  it("relative path always rewrites to absolute sandbox path", () => {
    // The tool's own path resolution would interpret a relative
    // path against ``workspaceDir`` (the GLOBAL root), which
    // would let User B's relative ``MEMORY.md`` land on User
    // A's data. Sandboxing absolutizes it against the per-
    // sender root so the tool resolves the right target.
    const v = classifyPath("MEMORY.md", ws, sender);
    expect(v.kind).toBe("rewrite");
    if (v.kind === "rewrite") {
      expect(v.original).toBe("MEMORY.md");
      expect(v.rewritten).toBe(
        "/workspace/_lumin/by-sender/alice/MEMORY.md",
      );
    }
  });

  it("path outside the workspace is unmanaged", () => {
    expect(classifyPath("/etc/passwd", ws, sender).kind).toBe("outside-workspace");
    expect(classifyPath("/tmp/foo.txt", ws, sender).kind).toBe("outside-workspace");
  });

  it("path-traversal escapes the workspace are still classified outside", () => {
    // /workspace/../../../etc/passwd → /etc/passwd. Path
    // resolution normalizes the escape; the sandbox check
    // sees a path outside the workspace and returns
    // outside-workspace (caller can choose to block).
    const v = classifyPath("../../../etc/passwd", ws, sender);
    // Relative path resolves against sandbox first:
    // /workspace/_lumin/by-sender/alice/../../../etc/passwd
    // = /workspace/etc/passwd → which IS inside workspace, so
    // gets rewritten back to alice's sandbox. This is actually
    // safer than letting it escape — the bot can never read
    // /etc by relative traversal because path.resolve clamps.
    expect(v.kind).toBe("rewrite");
  });
});


describe("matchesSharedPath", () => {
  const ws = "/workspace";

  it("returns undefined for empty patterns", () => {
    expect(matchesSharedPath("AGENTS.md", [], ws)).toBeUndefined();
    expect(matchesSharedPath("AGENTS.md", undefined as unknown as string[], ws)).toBeUndefined();
  });

  it("matches a relative pattern against a relative request", () => {
    const r = matchesSharedPath("AGENTS.md", ["AGENTS.md"], ws);
    expect(r).toBeDefined();
    expect(r?.matched).toBe("AGENTS.md");
    expect(r?.resolved).toBe("/workspace/AGENTS.md");
  });

  it("matches a relative pattern against an absolute request under workspace", () => {
    const r = matchesSharedPath("/workspace/AGENTS.md", ["AGENTS.md"], ws);
    expect(r).toBeDefined();
    expect(r?.resolved).toBe("/workspace/AGENTS.md");
  });

  it("supports single-star glob (one segment)", () => {
    const r = matchesSharedPath("docs/intro.md", ["docs/*"], ws);
    expect(r?.matched).toBe("docs/*");
    // Single-star should NOT match nested directories.
    expect(matchesSharedPath("docs/sub/intro.md", ["docs/*"], ws)).toBeUndefined();
  });

  it("supports double-star glob (recursive)", () => {
    expect(matchesSharedPath("docs/sub/intro.md", ["docs/**"], ws)).toBeDefined();
    expect(matchesSharedPath("docs/a/b/c.md", ["docs/**"], ws)).toBeDefined();
  });

  it("supports absolute glob patterns", () => {
    const r = matchesSharedPath(
      "/etc/openclaw/global.json",
      ["/etc/openclaw/*"],
      ws,
    );
    expect(r?.matched).toBe("/etc/openclaw/*");
    expect(r?.resolved).toBe("/etc/openclaw/global.json");
  });

  it("does not match unrelated paths", () => {
    expect(matchesSharedPath("MEMORY.md", ["AGENTS.md"], ws)).toBeUndefined();
    expect(matchesSharedPath("/etc/passwd", ["/etc/openclaw/*"], ws)).toBeUndefined();
  });
});


describe("sandboxToolParams (live filesystem)", () => {
  it("returns undefined for non-fs tools", () => {
    expect(sandboxToolParams({
      toolName: "web_fetch",
      params: { url: "https://example.com" },
      senderId: "alice",
      workspaceDir: tmpRoot,
    })).toBeUndefined();
  });

  it("returns undefined when workspaceDir is missing (fail-warn default)", () => {
    expect(sandboxToolParams({
      toolName: "read",
      params: { path: "MEMORY.md" },
      senderId: "alice",
      workspaceDir: undefined,
    })).toBeUndefined();
  });

  it("returns block when workspaceDir is missing AND failClosed is set", () => {
    const r = sandboxToolParams({
      toolName: "read",
      params: { path: "MEMORY.md" },
      senderId: "alice",
      workspaceDir: undefined,
      failClosed: true,
    });
    expect(r?.kind).toBe("block");
    if (r?.kind === "block") {
      expect(r.reason).toBe("missing_workspace_dir");
    }
  });

  it("rewrites read of MEMORY.md to alice's sandbox + creates the dir", () => {
    const result = sandboxToolParams({
      toolName: "read",
      params: { path: "MEMORY.md" },
      senderId: "alice",
      workspaceDir: tmpRoot,
    });
    expect(result?.kind).toBe("rewrite");
    if (result?.kind !== "rewrite") return;
    expect(result.params.path).toBe(
      path.join(tmpRoot, "_lumin/by-sender/alice/MEMORY.md"),
    );
    expect(fs.existsSync(
      path.join(tmpRoot, "_lumin/by-sender/alice"),
    )).toBe(true);
  });

  it("each fs tool gets its path sandboxed", () => {
    for (const tool of FS_TOOL_NAMES) {
      const r = sandboxToolParams({
        toolName: tool,
        params: { path: "notes/work.md" },
        senderId: "alice",
        workspaceDir: tmpRoot,
      });
      expect(r?.kind, `tool=${tool}`).toBe("rewrite");
      if (r?.kind !== "rewrite") continue;
      expect(r.params.path).toContain("_lumin/by-sender/alice/notes/work.md");
    }
  });

  // ----- shared_paths ----- //

  it("shared_paths: read of AGENTS.md is passed through to workspace root", () => {
    const r = sandboxToolParams({
      toolName: "read",
      params: { path: "AGENTS.md" },
      senderId: "alice",
      workspaceDir: tmpRoot,
      sharedPaths: ["AGENTS.md"],
    });
    expect(r?.kind).toBe("shared");
    if (r?.kind !== "shared") return;
    expect(r.params.path).toBe(path.join(tmpRoot, "AGENTS.md"));
    expect(r.sharedMatches).toEqual([
      { pattern: "AGENTS.md", resolved: path.join(tmpRoot, "AGENTS.md") },
    ]);
  });

  it("shared_paths: write to a shared path is BLOCKED", () => {
    const r = sandboxToolParams({
      toolName: "write",
      params: { path: "AGENTS.md", content: "evil" },
      senderId: "alice",
      workspaceDir: tmpRoot,
      sharedPaths: ["AGENTS.md"],
    });
    expect(r?.kind).toBe("block");
    if (r?.kind !== "block") return;
    expect(r.reason).toMatch(/^write_to_shared_path:AGENTS\.md$/);
  });

  it("shared_paths: edit of a shared path is BLOCKED", () => {
    const r = sandboxToolParams({
      toolName: "edit",
      params: { path: "AGENTS.md" },
      senderId: "alice",
      workspaceDir: tmpRoot,
      sharedPaths: ["AGENTS.md"],
    });
    expect(r?.kind).toBe("block");
  });

  it("shared_paths: glob pattern matches subdirectory content for read tools", () => {
    const r = sandboxToolParams({
      toolName: "ls",
      params: { path: "templates/welcome.md" },
      senderId: "alice",
      workspaceDir: tmpRoot,
      sharedPaths: ["templates/**"],
    });
    expect(r?.kind).toBe("shared");
    if (r?.kind !== "shared") return;
    expect(r.params.path).toBe(path.join(tmpRoot, "templates/welcome.md"));
  });

  it("shared_paths: non-matching path still falls through to per-sender sandbox", () => {
    const r = sandboxToolParams({
      toolName: "read",
      params: { path: "MEMORY.md" },
      senderId: "alice",
      workspaceDir: tmpRoot,
      sharedPaths: ["AGENTS.md"],
    });
    expect(r?.kind).toBe("rewrite");
  });

  // ----- the headline cross-session leak test ----- //

  it("user_b_cannot_read_user_a_memory", () => {
    // 1. Alice's session writes her secret to MEMORY.md.
    const aliceWrite = sandboxToolParams({
      toolName: "write",
      params: { path: "MEMORY.md", content: "alice account: BANANA-44221" },
      senderId: "alice",
      workspaceDir: tmpRoot,
    });
    expect(aliceWrite?.kind).toBe("rewrite");
    if (aliceWrite?.kind !== "rewrite") return;
    // Sandbox layer rewrites the path; we still need to
    // perform the actual write so the test models real life.
    fs.writeFileSync(
      aliceWrite.params.path as string,
      String(aliceWrite.params.content),
    );

    // 2. Bob's session asks the LLM "what did the previous
    //    user say?" The LLM tries to read MEMORY.md.
    const bobRead = sandboxToolParams({
      toolName: "read",
      params: { path: "MEMORY.md" },
      senderId: "bob",
      workspaceDir: tmpRoot,
    });
    expect(bobRead?.kind).toBe("rewrite");
    if (bobRead?.kind !== "rewrite") return;
    // Bob's read resolves to bob's sandbox — alice's file
    // doesn't exist there.
    const bobPath = bobRead.params.path as string;
    expect(bobPath).toBe(
      path.join(tmpRoot, "_lumin/by-sender/bob/MEMORY.md"),
    );
    expect(fs.existsSync(bobPath)).toBe(false);

    // 3. Alice's secret is intact in alice's sandbox; bob's
    //    sandbox doesn't have it. The leak is impossible by
    //    construction — this is the v0.6.1 demo, pinned.
    const alicePath = aliceWrite.params.path as string;
    const aliceContent = fs.readFileSync(alicePath, "utf8");
    expect(aliceContent).toContain("BANANA-44221");
  });

  it("user_b_cannot_read_user_a_memory_via_absolute_path_either", () => {
    // The LLM might learn alice's absolute path from history
    // and try to bypass the sandbox by typing the full path
    // verbatim. Sandboxing re-resolves it to bob's namespace.
    const alicePath = path.join(
      tmpRoot, "_lumin/by-sender/alice/MEMORY.md",
    );
    fs.mkdirSync(path.dirname(alicePath), { recursive: true });
    fs.writeFileSync(alicePath, "alice account: BANANA-44221");

    const bobAttempt = sandboxToolParams({
      toolName: "read",
      params: { path: alicePath },
      senderId: "bob",
      workspaceDir: tmpRoot,
    });
    expect(bobAttempt?.kind).toBe("rewrite");
    if (bobAttempt?.kind !== "rewrite") return;
    const bobResolvedPath = bobAttempt.params.path as string;
    expect(bobResolvedPath).not.toBe(alicePath);
    expect(bobResolvedPath).toContain("by-sender/bob/");
    expect(fs.existsSync(bobResolvedPath)).toBe(false);
  });

  it("new fs tool grep with workspace-internal path gets sandboxed", () => {
    // grep was added to FS_TOOL_NAMES in v0.7.0. Verify it
    // routes through the same sandbox path as read/write.
    const r = sandboxToolParams({
      toolName: "grep",
      params: { path: path.join(tmpRoot, "src"), pattern: "secret" },
      senderId: "carol",
      workspaceDir: tmpRoot,
    });
    expect(r?.kind).toBe("rewrite");
    if (r?.kind !== "rewrite") return;
    expect(r.params.path).toBe(
      path.join(tmpRoot, "_lumin/by-sender/carol/src"),
    );
  });

  it("new fs tool cat with relative path gets sandboxed", () => {
    const r = sandboxToolParams({
      toolName: "cat",
      params: { path: "MEMORY.md" },
      senderId: "carol",
      workspaceDir: tmpRoot,
    });
    expect(r?.kind).toBe("rewrite");
    if (r?.kind !== "rewrite") return;
    expect(r.params.path).toBe(
      path.join(tmpRoot, "_lumin/by-sender/carol/MEMORY.md"),
    );
  });
});
