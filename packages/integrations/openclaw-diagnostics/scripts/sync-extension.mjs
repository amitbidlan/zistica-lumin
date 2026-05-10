#!/usr/bin/env node
/**
 * After-build sync: copies the freshly compiled dist + the
 * openclaw.plugin.json manifest into the locally-installed extension
 * directory so the running gateway picks them up on next register.
 *
 * Skips silently when the extension dir doesn't exist (e.g., CI
 * builds, fresh clone before `openclaw extension install`). Runs as
 * the ``postbuild`` step via ``npm run build``.
 *
 * Why this matters: the manifest's ``configSchema`` declares
 * ``additionalProperties: false`` — every config field added to the
 * plugin source MUST also land in the manifest, or OpenClaw
 * silently drops the plugin entry on next start. Catching that drift
 * by hand was a footgun (we hit it twice in 2026-05). Building it
 * into the build pipeline removes the manual step.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";

const HOME = os.homedir();
const EXT_ROOT = process.env.LUMIN_OPENCLAW_EXT_DIR
  ?? path.join(HOME, ".openclaw", "extensions", "lumin-diagnostics");

const SRC_ROOT = path.resolve(import.meta.dirname ?? path.dirname(new URL(import.meta.url).pathname), "..");

if (!fs.existsSync(EXT_ROOT)) {
  console.log(`[sync-extension] no extension dir at ${EXT_ROOT}; skipping`);
  process.exit(0);
}

function copyTree(src, dst) {
  if (!fs.existsSync(src)) return;
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dst, { recursive: true });
    for (const entry of fs.readdirSync(src)) {
      copyTree(path.join(src, entry), path.join(dst, entry));
    }
  } else {
    fs.copyFileSync(src, dst);
  }
}

const SRC_DIST = path.join(SRC_ROOT, "dist");
const DST_DIST = path.join(EXT_ROOT, "dist");
const SRC_MANIFEST = path.join(SRC_ROOT, "openclaw.plugin.json");
const DST_MANIFEST = path.join(EXT_ROOT, "openclaw.plugin.json");

if (fs.existsSync(SRC_DIST)) {
  copyTree(SRC_DIST, DST_DIST);
  console.log(`[sync-extension] dist → ${DST_DIST}`);
}
if (fs.existsSync(SRC_MANIFEST)) {
  fs.copyFileSync(SRC_MANIFEST, DST_MANIFEST);
  console.log(`[sync-extension] manifest → ${DST_MANIFEST}`);
}
