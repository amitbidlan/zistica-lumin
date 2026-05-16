# Show HN draft

Pair with the GIF from `launch/record_demo.sh`. HN rewards technical and
honest; it punishes hype. This draft is deliberately plain.

---

**Title (≤80 chars, pick one):**

- `Show HN: Lumin – watch your AI agent get hacked, then block it, on localhost`
- `Show HN: Local-first firewall + tracing for AI agents (one command, no keys)`

> Title rule: lead with the visible thing (the block), not the category.

---

**Body:**

Lumin is a local-first observability + firewall for AI agents. `pip install`,
`lumin up`, `lumin demo` — the demo instruments a real agent, prompt-injects
it into `rm -rf` and reading `~/.aws/credentials`, and you watch the firewall
block both in the terminal and as clickable rows in the dashboard. No account,
no API key, no data leaves the machine.

Why we built it: observability tools (Langfuse/Phoenix) tell you what your
agent did *after* it did it. Guardrail classifiers score one prompt in
isolation. We wanted one thing you run in front of the agent that records
*and* enforces — locally, so it works air-gapped and in regulated shops where
cloud SaaS is a non-starter.

Some specifics HN may care about:

- One container (FastAPI + Next.js + DuckDB), drop-on-overflow exporter — a
  Lumin outage can never break your agent (hard rule).
- Policy engine with shadow→enforce lifecycle; rules ship in shadow and
  record what they *would* do before you promote them.
- `lumin scorecard --target <url>` replays the OWASP LLM Top-10 attack suite
  at any OpenAI-compatible agent and grades it → a shareable badge.
- 313 tests; the CLI demo above is verified end-to-end (the blocks are real
  rows, not a mock).

Honest limitations:

- `pip install lumin` is taken on PyPI by an unrelated project, so install
  is currently a Git URL (we're fixing the distribution name — issue #120).
- The dashboard has no auth by default (localhost story); bind to 127.0.0.1
  or set `LUMIN_DASHBOARD_PASSWORD`. Safe-by-default for remote binds is on
  the roadmap.
- The big ML detectors (Prompt Guard 2, Llama Guard 4) are optional; without
  them those rules no-op (the regex/structural ones still work). `lumin
  doctor` tells you exactly what's loaded.

Apache-2.0, single founder, very early. Repo + roadmap:
https://github.com/amitbidlan/zistica-lumin

Happy to go deep on the firewall decision engine, the local classifier, or
the wire format in the comments.

---

**First-comment (post immediately, pre-empts the obvious questions):**

Architecture: agent → bounded SDK queue (drop on overflow) → POST /v1/spans
→ FastAPI → DuckDB + WebSocket fanout to the Next.js dashboard. The firewall
runs at five lifecycle hooks (before/after proxy + tool call, post-ingest)
with a per-lifecycle latency budget; on timeout it fails open (allow) so it
can't wedge your agent. Conditions are a sandboxed expression DSL
(simpleeval, never `eval`). Ask me anything.

---

**Where to post (in order):** HN Show HN (Tue–Thu, ~8am ET) → r/LocalLLaMA
(lean on air-gapped/no-egress) → the scorecard badge on X. Have the GIF, the
asciinema link, and `lumin doctor` output ready before posting.
