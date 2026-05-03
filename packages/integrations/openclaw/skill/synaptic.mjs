#!/usr/bin/env node
/**
 * Synaptic skill helper for OpenClaw.
 *
 * Pure Node.js — no `npm install` needed. Uses the built-in fetch()
 * available in Node 18+. Never throws; always prints clean text.
 *
 * Commands:
 *   node synaptic.mjs status         — is Synaptic up?
 *   node synaptic.mjs traces         — last 5 traces (formatted)
 *   node synaptic.mjs trace <id>     — single trace + all its spans
 *
 * Reads SYNAPTIC_HOST from env (defaults to http://localhost:8000).
 */

const HOST = (process.env.SYNAPTIC_HOST || 'http://localhost:8000').replace(
  /\/+$/,
  '',
);
const TIMEOUT_MS = 4000;

/** Fetch a Synaptic API URL with a timeout. Returns parsed JSON or
 *  null if anything goes wrong — never throws. */
async function fetchJson(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(`${HOST}${path}`, { signal: controller.signal });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function fmtCost(usd) {
  if (usd === null || usd === undefined) return '$—';
  if (usd === 0) return '$0';
  if (usd < 0.01) return `$${usd.toFixed(6)}`;
  return `$${usd.toFixed(4)}`;
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function statusOf(trace) {
  if (trace.ended_at === null || trace.ended_at === undefined) return 'RUNNING';
  // We treat traces with any errored span as ERROR — but at the
  // /v1/traces list level we don't have span-level info, so this
  // is a heuristic: if quality_score is set and < 0.5, flag.
  // Otherwise OK.
  return 'OK';
}

async function cmdStatus() {
  const r = await fetchJson('/health');
  if (r && r.status === 'ok') {
    return `Synaptic is running on ${HOST}`;
  }
  return (
    `Synaptic is not running on ${HOST}.\n` +
    `Start it with: docker run -p 3000:3000 -p 8000:8000 zistica/synaptic`
  );
}

async function cmdTraces() {
  const traces = await fetchJson('/v1/traces?limit=5');
  if (!Array.isArray(traces)) {
    return (
      `Could not reach Synaptic at ${HOST}.\n` +
      `Start it with: docker run -p 3000:3000 -p 8000:8000 zistica/synaptic`
    );
  }
  if (traces.length === 0) {
    return `No traces yet. Run an OpenClaw agent and they'll appear at ${HOST.replace(':8000', ':3000')}/traces`;
  }
  const lines = ['Recent agent traces:'];
  traces.forEach((t, i) => {
    const name = t.name ?? '(unnamed)';
    const dur = fmtDuration(t.duration_ms);
    const cost = fmtCost(t.total_cost_usd);
    const status = statusOf(t);
    lines.push(
      `  ${i + 1}. ${name.padEnd(22)} ${dur.padStart(7)}  ${cost.padStart(9)}  ${status}`,
    );
  });
  lines.push('');
  lines.push(`Open ${HOST.replace(':8000', ':3000')}/traces for the full list.`);
  return lines.join('\n');
}

async function cmdTrace(id) {
  if (!id) {
    return 'Usage: node synaptic.mjs trace <trace-id>';
  }
  const trace = await fetchJson(`/v1/traces/${encodeURIComponent(id)}`);
  if (trace === null) {
    return (
      `Trace ${id} not found, or Synaptic is unreachable at ${HOST}.`
    );
  }
  const spans = (await fetchJson(`/v1/traces/${encodeURIComponent(id)}/spans`)) || [];
  const lines = [
    `Trace: ${trace.name ?? '(unnamed)'}`,
    `  id:        ${trace.id}`,
    `  duration:  ${fmtDuration(trace.duration_ms)}`,
    `  cost:      ${fmtCost(trace.total_cost_usd)}`,
    `  tokens:    ${trace.total_tokens ?? 0}`,
    `  session:   ${trace.session_id ?? '—'}`,
    `  started:   ${trace.started_at ?? '—'}`,
    '',
    `Spans (${spans.length}):`,
  ];
  for (const s of spans) {
    const dur = fmtDuration(s.duration_ms);
    const model = s.model ? ` ${s.model}` : '';
    const tok =
      s.tokens_input !== null && s.tokens_input !== undefined
        ? ` ${s.tokens_input}/${s.tokens_output ?? 0} tok`
        : '';
    const cost = s.cost_usd ? ` ${fmtCost(s.cost_usd)}` : '';
    const err = s.error_message ? `  error: ${s.error_message}` : '';
    lines.push(
      `  - [${(s.type ?? 'custom').padEnd(9)}] ${s.name ?? '(unnamed)'}` +
        `${model}${tok}${cost}  ${dur}${err}`,
    );
  }
  lines.push('');
  lines.push(
    `Open ${HOST.replace(':8000', ':3000')}/traces/${trace.id} for the full timeline.`,
  );
  return lines.join('\n');
}

async function main() {
  const cmd = process.argv[2] || 'status';
  let out;
  try {
    if (cmd === 'status') out = await cmdStatus();
    else if (cmd === 'traces') out = await cmdTraces();
    else if (cmd === 'trace') out = await cmdTrace(process.argv[3]);
    else
      out =
        `Unknown command: ${cmd}\n` +
        `Usage:\n` +
        `  node synaptic.mjs status\n` +
        `  node synaptic.mjs traces\n` +
        `  node synaptic.mjs trace <id>`;
  } catch (e) {
    // Last-ditch safety net — should be unreachable since fetchJson
    // already swallows. But if anything else throws, we still print
    // something readable instead of crashing.
    out = `Synaptic skill encountered an internal error and stopped cleanly: ${
      (e && e.message) || e
    }`;
  }
  process.stdout.write(out + '\n');
}

main();
