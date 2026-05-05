# Lumin

> Local-first AI agent observability. Runs on your laptop. No account. No data leaves your machine.

[![CI](https://github.com/amitbidlan/zistica-lumin/actions/workflows/ci.yml/badge.svg)](https://github.com/amitbidlan/zistica-lumin/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![Node](https://img.shields.io/badge/node-18+-green.svg)](https://nodejs.org)

You build an AI agent. It works in testing. You deploy it. A user complains it gave a wrong answer. You have no idea why — which step failed, what data it used, where it went wrong.

Lumin is a CCTV camera for your AI agent. Add 2 lines of code. Every decision, every tool call, every LLM response — recorded, visible, debuggable.

---

## Quick start

```bash
git clone https://github.com/amitbidlan/zistica-lumin
cd zistica-lumin
docker build -t lumin .
docker run -p 3000:3000 -p 8000:8000 -v $(pwd)/data:/data lumin
```

Both ports matter:
- **`3000`** — the dashboard (this is what you open in the browser)
- **`8000`** — the API. The browser also connects to it directly for the WebSocket real-time stream. Skip this mapping and the dashboard still works, but new traces appear via 5-second polling instead of live push.

Open <http://localhost:3000> — the agent grid (`/agents`) is the dashboard home. From there you can drill into individual agents, traces, sessions, and policy violations.

Then in your agent:

```python
import lumin

@lumin.trace
def my_agent(question: str) -> str:
    docs = search_documents(question)
    return gpt4_answer(question, docs)

my_agent("What is the capital of France?")
# Trace appears in the dashboard.
```

That's it. No account, no API key, no data leaves your laptop.

---

## Preview

![Trace list — paginated, auto-refreshing every 5 seconds](assets/dashboard-trace.png)

*Trace list — every agent run on a paginated, live-refreshing table.*

![Trace detail — span timeline with expanded input/output](assets/dashboard-details.png)

*Trace detail — click any span in the timeline to inspect its input/output JSON. Errors render in red.*

---

## What you get

- **Agent grid** — `/agents` is the dashboard home. Every distinct trace name appears as an agent card, grouped by integration framework (OpenClaw / Mastra / VoltAgent / Python SDK). Each card shows last-seen activity, traces, cost, top model, and a 60-minute sparkline. Long-tail sections cap at 6 cards with a "view all" tile that drops you into the dedicated framework page.
- **Live indicators** — every card has an activity dot (active / idle / dormant) that flips on WebSocket events without a page refresh. A "thinking" pill pulses on cards with traces in flight. Polling fallback when WS disconnects.
- **Policy engine** — `/policies` UI to create, edit, and delete rules that fire when an agent misbehaves (cost runaways, prompt-injection patterns, PII in output, runaway loops). Rules are DB-backed with a full audit log; per-agent scoping via `scope.agents: [...]`. The engine evaluates server-side after every span/trace ingest, so TS integrations get the same enforcement as the Python SDK.
- **Span timeline** — every LLM call, tool invocation, retrieval, and custom span rendered as an indented tree, click any span to expand its input/output JSON.
- **Multi-turn sessions** — group related traces under a single conversation; the dashboard's `/sessions` view shows turns in chronological order with aggregate cost, duration, and quality.
- **Real-time updates** — WebSocket stream pushes traces and spans into the dashboard the moment they're ingested, no refresh; falls back to 5-second polling if the socket can't connect.
- **Cost & token tracking** — automatic per-call breakdown for OpenAI and Anthropic model families.
- **Quality scoring** — bring-your-own evals via `POST /v1/evals`.
- **Framework integrations** — drop-in support for [LangChain](https://github.com/langchain-ai/langchain) (zero-config via `LUMIN_TRACING=true` — see [section](#langchain)), [LlamaIndex](https://github.com/run-llama/llama_index) ([section](#llamaindex)), [CrewAI](https://github.com/crewAIInc/crewAI) ([section](#crewai)), [Anthropic](https://github.com/anthropics/anthropic-sdk-python) with extended-thinking visualization ([section](#anthropic)), [Mastra](https://github.com/mastra-ai/mastra) ([`@lumin-io/mastra`](packages/integrations/mastra/)), [OpenClaw](https://github.com/openclaw/openclaw) (zero-code via `diagnostics-otel` → Lumin's OTLP endpoint, see [section](#openclaw)), and [VoltAgent](https://github.com/voltagent/voltagent) ([`@lumin-io/voltagent`](packages/integrations/voltagent/)).
- **Cross-language SDKs** — Python and TypeScript with identical wire format and behavior.
- **Resilient by design** — the agent never fails because Lumin is down. Spans drop silently if the queue overflows, the exporter is unreachable, or the server returns an error.
- **Local-first** — single Docker image, DuckDB + SQLite, no external services, no cloud dependency.
- **90-day retention** — automatic cleanup task keeps the database from growing unbounded (configurable via env var).

---

## Architecture

```
   Agent code (Python or TypeScript)
        │  @lumin.trace / trace(fn)
        ▼
   SDK queue (bounded, drop-on-overflow)
        │  background async exporter
        ▼
   HTTP POST /v1/spans  ───►  FastAPI ──┐
                                 │      │ broadcast
                                 ▼      ▼
                              DuckDB    /ws/traces (WebSocket fanout)
                              SQLite       │
                                 ▲         │  push: new_trace, new_span
                                 │         │
                       GET /v1/* │         ▼
   Browser  ──►  Next.js Dashboard  ──◄────┘
                  localhost:3000
                  /api/* (HTTP rewrite proxy)
```

Single Docker container runs the API on `:8000` and the Next.js standalone dashboard on `:3000`. HTTP requests go through the dashboard's `/api/*` rewrite proxy (so the browser never crosses origins for HTTP). WebSocket connects directly to `:8000/ws/traces` — Next.js rewrites are HTTP-only, so port `8000` must be exposed for real-time updates (the dashboard falls back to 5-second polling otherwise).

---

## Packages

| Path | Package | Tests |
|---|---|---|
| [`packages/sdk-python/`](packages/sdk-python/) | Python SDK — `@lumin.trace`, `lumin.span()`, `lumin.session()`, policy engine, framework integrations | 209 |
| [`packages/sdk-typescript/`](packages/sdk-typescript/) | `@lumin-io/sdk` — peer of the Python SDK | 59 |
| [`packages/api/`](packages/api/) | FastAPI ingest + query API, DuckDB storage, WebSocket fanout, agent grid, server-side policy engine | 169 |
| [`packages/integrations/openclaw/`](packages/integrations/openclaw/) | `@lumin-io/openclaw` — OTel exporter for OpenClaw agents | 59 |
| [`packages/integrations/mastra/`](packages/integrations/mastra/) | `@lumin-io/mastra` — observability config + exporter for Mastra | 55 |
| [`packages/integrations/voltagent/`](packages/integrations/voltagent/) | `@lumin-io/voltagent` — OTel-native exporter for VoltAgent | 62 |
| [`packages/dashboard/`](packages/dashboard/) | Next.js 14 dashboard — agent grid, traces, sessions, violations, policy editor | build + 21 Playwright E2E |

---

## Install

### Whole workspace (recommended for contributors)

```bash
./setup.sh
```

One-shot installer: creates `packages/api/.venv`, installs the local Python SDK editable, installs API dependencies, runs `npm ci` across all 5 Node packages — in the right order. Required because the API imports `lumin` from the sibling `packages/sdk-python`, not from PyPI (where an unrelated package shares the name).

### Python SDK

```bash
pip install -e packages/sdk-python              # core only
pip install -e packages/sdk-python[langchain]   # + LangChain integration
pip install -e packages/sdk-python[crewai]      # + CrewAI integration
pip install -e packages/sdk-python[anthropic]   # + Anthropic integration
pip install -e packages/sdk-python[llama_index] # + LlamaIndex integration
pip install -e packages/sdk-python[all]         # all integrations
```

### TypeScript SDK

```bash
# Build the SDK locally (it's not yet published to npm):
cd packages/sdk-typescript && npm install && npm run build

# Then in your project, link or install from the path:
npm install /absolute/path/to/zistica-lumin/packages/sdk-typescript
```

### Whole stack via Docker

Already covered in [Quick start](#quick-start). One image, both API and dashboard.

---

## Usage

### Python — decorator

```python
import lumin

lumin.configure(host="http://localhost:8000")  # or set LUMIN_HOST

@lumin.trace
def my_agent(input: str) -> str:
    return "hello"

my_agent("test")  # trace appears in the dashboard
```

### Python — context manager (manual span)

```python
with lumin.span("retrieval", type="retrieval") as s:
    results = vector_db.search(query)
    s.set_output({"count": len(results)})
```

### TypeScript

```typescript
import { configure, trace } from '@lumin-io/sdk';

configure({ host: 'http://localhost:8000' });

const myAgent = trace(async (input: string) => {
  return 'hello';
}, { name: 'my_agent' });

await myAgent('test');
```

### LangChain

Works with [LangChain](https://github.com/langchain-ai/langchain) (Python). Zero-config — set the env var before importing LangChain and every chain is auto-traced:

```python
import os
os.environ["LUMIN_HOST"] = "http://localhost:8000"
os.environ["LUMIN_TRACING"] = "true"

from langchain_openai import ChatOpenAI
ChatOpenAI().invoke("Hello")  # span recorded with model, tokens, cost
```

Or attach the handler explicitly:

```python
from lumin.integrations.langchain import LuminCallbackHandler

handler = LuminCallbackHandler()
llm = ChatOpenAI(callbacks=[handler])
```

### CrewAI

For [CrewAI](https://github.com/crewAIInc/crewAI) multi-agent crews:

```python
from lumin.integrations.crewai import instrument_crew
instrument_crew()

from crewai import Agent, Task, Crew
crew = Crew(agents=[...], tasks=[...])
crew.kickoff()
# crew.kickoff -> root span
# each agent.execute_task -> child span
# LLM calls inside agents -> grandchildren (via the LangChain integration)
```

### LlamaIndex

For [LlamaIndex](https://github.com/run-llama/llama_index) RAG pipelines and agents:

```python
from lumin.integrations.llama_index import LuminCallbackHandler
from llama_index.core import Settings, VectorStoreIndex, Document
from llama_index.core.callbacks import CallbackManager

handler = LuminCallbackHandler()
Settings.callback_manager = CallbackManager([handler])

index = VectorStoreIndex.from_documents([Document(text="…")])
index.as_query_engine().query("what is …?")
# query (root)
#   retrieve (retrieval)  -- retrieved nodes + similarity scores
#     embedding (embedding) -- query embedding model + tokens
#   synthesize (custom)
#     llm_call (llm)       -- model, tokens, cost
```

### Anthropic

For [Anthropic Claude](https://github.com/anthropics/anthropic-sdk-python) — captures extended-thinking blocks as first-class child spans:

```python
from lumin.integrations.anthropic import instrument_anthropic
instrument_anthropic()

from anthropic import Anthropic
client = Anthropic()
client.messages.create(
    model="claude-opus-4-20250514",
    max_tokens=16000,
    thinking={"type": "enabled", "budget_tokens": 10000},
    messages=[{"role": "user", "content": "..."}],
)
# claude_call -> parent (model, tokens, cost)
#   thinking -> child (reasoning text, ~thinking tokens, output-rate cost)
#   response -> child (final text, response tokens, cost)
# Dashboard renders thinking rows with a brain emoji + per-trace
# thinking-vs-response cost breakdown.
```

### Mastra

For [Mastra](https://github.com/mastra-ai/mastra) (TypeScript agent framework). Drop-in replacement for `@mastra/langfuse` if you want local-first observability without sending data to a hosted SaaS:

```typescript
import { Mastra } from '@mastra/core';
import { luminConfig } from '@lumin-io/mastra';

export const mastra = new Mastra({
  agents: { myAgent },
  observability: luminConfig({ serviceName: 'my-mastra-app' }),
});
```

See [`packages/integrations/mastra/`](packages/integrations/mastra/) for the dedicated `@lumin-io/mastra` package.

### OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) ships native OpenTelemetry through its `diagnostics-otel` plugin — Lumin's OTLP/HTTP endpoint receives those traces directly, so **no agent-code changes, no `@lumin.trace`, no SDK install**.

```bash
# Step 1 — start Lumin
docker run -p 3000:3000 -p 8000:8000 zistica/lumin

# Step 2 — enable diagnostics + point OpenClaw at Lumin
openclaw config set diagnostics.enabled true
openclaw config set diagnostics.otel.enabled true
openclaw config set diagnostics.otel.traces true
openclaw config set diagnostics.otel.endpoint "http://localhost:8000/v1/otlp"
openclaw gateway restart

# Step 3 — open http://localhost:3000
# every OpenClaw run appears automatically
```

OpenClaw's `diagnostics-otel` auto-appends `/v1/traces` to the configured base, so the POST lands at Lumin's OTLP route `http://localhost:8000/v1/otlp/v1/traces`. (If your OpenClaw is v2026.4.25+ and you prefer the signal-specific form, use `diagnostics.otel.traces.endpoint "http://localhost:8000/v1/otlp/v1/traces"` instead.)

**What gets captured**
- LLM calls — model, tokens, cost, duration
- Tool calls — file, shell, web, email
- Agent sessions end-to-end as a span tree
- Policy violations auto-detected by Lumin's policy engine

For an SDK-style integration with extra features (custom span subtypes, client-side cost calculation, span-name normalization), see [`@lumin-io/openclaw`](packages/integrations/openclaw/) — same end result, runs as an in-process exporter rather than a network hop.

### Sessions (multi-turn conversations)

Group several agent calls into one logical conversation:

```python
import lumin

with lumin.session(name="booking-conversation"):
    my_agent("Book me a flight to Tokyo")
    my_agent("Make it business class")
    my_agent("Add a hotel for 3 nights")
```

Every traced call inside the `with` block is tagged with a shared `session_id`. The dashboard groups them under `/sessions` with aggregate cost / duration / quality. Click into a session to see the turns in order, click any turn to expand its full span tree.

TypeScript:

```typescript
import { withSession, trace } from '@lumin-io/sdk';

const myAgent = trace(async (q: string) => '...', { name: 'agent' });

await withSession({ name: 'booking-conversation' }, async () => {
  await myAgent('Book me a flight');
  await myAgent('Make it business class');
});
```

Or pin a specific session at decoration time:

```python
@lumin.trace(session_id="user-123-conv-456")
def my_agent(q): ...
```

Resolution priority for a span's `session_id` (most specific wins): explicit `@trace(session_id=…)` > active `lumin.session()` context > parent span's `session_id` > `None`. Traces without a `session_id` stay in `/traces` as standalone runs and don't appear under any session.

### Policy engine

Define rules that fire when an agent misbehaves. Manage them in the dashboard at `/policies` (UI editor with audit log) or via a YAML file at startup (`LUMIN_POLICY_FILE=policies.yaml`). The first time the engine starts with a YAML file and an empty DB, the rules are imported once; from then on the DB is the source of truth.

```yaml
version: 1
policies:
  - name: cost_runaway
    description: Trace cost exceeded $0.50 — likely runaway loop or context bloat
    trigger: trace_end
    condition: "trace.total_cost_usd > 0.50"
    action: alert
    severity: high

  - name: pii_leak_email_in_response
    trigger: span_end
    condition: "span.type == 'llm' and '@' in str(span.output) and '.com' in str(span.output)"
    action: flag
    severity: medium

  - name: slow_billing_only
    description: Only fires for the billing agent
    trigger: span_end
    condition: "span.type == 'llm' and span.duration_ms > 15000"
    action: alert
    severity: medium
    scope:
      agents:
        - billing_agent
```

Conditions are evaluated by [simpleeval](https://github.com/danthedeckie/simpleeval) — safe expressions only, no `eval()`. Available identifiers: `span` and `trace` (with `.duration_ms`, `.total_cost_usd`, `.span_count`, `.error_count`, `.tokens_input/output`, `.model`, `.input`, `.output`, `.type`, etc.). Available functions: `len`, `str`, `int`, `float`, `abs`. Anything else (`__import__`, `open`, method calls) is rejected at write time.

`action: alert` POSTs the violation payload to the policy's `webhook_url` (or the global `lumin.configure(alert_webhook=…)` fallback). `action: flag` is silent — visible in `/violations` but no webhook fired. Violations are visible in three places:

- A red badge on the agent's card on `/agents`
- The `/violations` page (filterable by severity, policy name)
- The `Policy` tab on each trace detail page

`scope.agents: [name1, name2]` (Phase 3) limits a rule to specific agents. Empty / omitted means it applies to every agent. The agent identity is `trace.name` — see the [agent grid](#what-you-get) section.

---

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `LUMIN_HOST` | `http://localhost:8000` | API endpoint the SDK posts to |
| `LUMIN_API_KEY` | _(none)_ | Sent as `X-API-Key` header |
| `LUMIN_PROJECT` | `default` | Project identifier. Sent as the `X-Lumin-Project` header at ingest. The API normalizes to one of `{openclaw, mastra, voltagent, default}` — anything else folds to `default`. |
| `LUMIN_TRACING` | _(unset)_ | Set to `true` to auto-attach the LangChain handler |
| `LUMIN_DATA_DIR` | `./data` (or `/data` in Docker) | Where DuckDB + SQLite files live |
| `LUMIN_RETENTION_DAYS` | `90` | Max age of traces before automatic cleanup |
| `LUMIN_CLEANUP_INTERVAL_HOURS` | `24` | How often the cleanup task sweeps |
| `LUMIN_CLEANUP_ENABLED` | `true` | Set to `false` to disable retention cleanup |
| `LUMIN_POLICY_FILE` | _(none)_ | Path to a YAML file used to bootstrap the policy DB on first start. Once policies exist in the DB they take over; the file becomes informational. |
| `LUMIN_POLICY_WATCH` | `true` | Background watcher that reloads the engine when the DB token (row count + max version) changes, or when `LUMIN_POLICY_FILE`'s mtime changes. |
| `LUMIN_POLICY_WATCH_INTERVAL` | `30` (seconds) | Watcher poll cadence. Lower = faster reload, higher = less DB chatter. |

---

## Development

The fastest path is `./setup.sh` from the repo root — it handles the dependency-order dance across Python + 5 Node packages. Per-package commands if you prefer:

```bash
# Python SDK
cd packages/sdk-python
python -m venv .venv && .venv/bin/pip install -e ".[test,langchain]"
.venv/bin/pytest

# FastAPI — the local SDK MUST be installed first or the API can't import lumin.
cd packages/api
python -m venv .venv
.venv/bin/pip install -e ../sdk-python
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest

# TypeScript SDK
cd packages/sdk-typescript
npm ci && npm run build && npm test

# TS integrations (each has its own test suite)
cd packages/integrations/openclaw  && npm ci && npm test
cd packages/integrations/mastra    && npm ci && npm test
cd packages/integrations/voltagent && npm ci && npm test

# Dashboard
cd packages/dashboard
npm ci && npm run dev   # http://localhost:3000

# Dashboard E2E (browser tests, requires the Docker container running)
cd packages/dashboard
npm run test:e2e:install     # one-time: download Chromium
npm run test:e2e             # 21 Playwright tests against localhost
```

CI runs 9 jobs on every push and pull request — Python SDK, FastAPI, TypeScript SDK, three integration suites (OpenClaw / Mastra / VoltAgent), Next.js dashboard build, Docker image build + smoke, and Playwright E2E (real Chromium against the live container). See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## API reference (selected)

```
# Ingest + traces
POST   /v1/spans              # SDKs send here
POST   /v1/traces             # Manual upsert
GET    /v1/traces             # Paginated list (?limit=&offset=)
GET    /v1/traces/{id}        # Single trace
GET    /v1/traces/{id}/spans  # All spans for a trace

# Agents (derived from trace.name)
GET    /v1/agents             # Grid; ?window_hours=, ?search=, ?project=, ?provider=
GET    /v1/agents/{name}      # Detail: recent traces + violation breakdown

# Sessions
GET    /v1/sessions           # Derived via GROUP BY session_id
GET    /v1/sessions/{id}      # Session detail with all traces in order

# Policies (DB-backed CRUD; falls back to YAML when DB is empty)
GET    /v1/policies           # ?agent= filters to rules that fire for that agent
GET    /v1/policies/{name}
POST   /v1/policies           # Validates condition through SimpleEval at write
PUT    /v1/policies/{name}    # Bumps version, writes audit row
DELETE /v1/policies/{name}    # Soft-delete (enabled=false)
GET    /v1/policies/{name}/audit
GET    /v1/policy/metrics     # Engine state + p50/p99 eval latency
POST   /v1/policy/reload      # Force a hot-reload

# Violations
GET    /v1/violations         # ?severity=, ?policy_name=, ?trace_id=
GET    /v1/violations/stats   # Counts by severity + policy

# Evals
POST   /v1/evals              # Submit a quality score

# Misc
WS     /ws/traces             # Real-time fanout — new_trace + new_span events
GET    /health                # Liveness check
GET    /docs                  # Auto-generated Swagger UI (also at /api/docs through dashboard)
```

**WebSocket message shape:**

```json
{ "type": "new_span",  "trace_id": "...", "span":  { ...full span... } }
{ "type": "new_trace",                    "trace": { ...full trace... } }
```

`new_trace` fires when a trace is first created **or** when a root span (`parent_span_id == null`) lands on an existing stub — so the dashboard always sees the populated trace, not just the stub.

---

## License

[Apache License 2.0](LICENSE) — free to use, fork, modify, and distribute.

---

## Community

- **Code of Conduct** — see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Reports to `support@zistica.com`.
- **Security** — see [SECURITY.md](SECURITY.md). Please report vulnerabilities privately.
- **Contributing** — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are labeled in the [tracker](https://github.com/amitbidlan/zistica-lumin/issues).
