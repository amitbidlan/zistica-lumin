# Synaptic

> Local-first AI agent observability. Runs on your laptop. No account. No data leaves your machine.

[![CI](https://github.com/amitbidlan/zistica-synaptic/actions/workflows/ci.yml/badge.svg)](https://github.com/amitbidlan/zistica-synaptic/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![Node](https://img.shields.io/badge/node-18+-green.svg)](https://nodejs.org)

You build an AI agent. It works in testing. You deploy it. A user complains it gave a wrong answer. You have no idea why — which step failed, what data it used, where it went wrong.

Synaptic is a CCTV camera for your AI agent. Add 2 lines of code. Every decision, every tool call, every LLM response — recorded, visible, debuggable.

---

## Quick start

```bash
git clone https://github.com/amitbidlan/zistica-synaptic
cd zistica-synaptic
docker build -t synaptic .
docker run -p 3000:3000 -p 8000:8000 -v $(pwd)/data:/data synaptic
```

Both ports matter:
- **`3000`** — the dashboard (this is what you open in the browser)
- **`8000`** — the API. The browser also connects to it directly for the WebSocket real-time stream. Skip this mapping and the dashboard still works, but new traces appear via 5-second polling instead of live push.

Open <http://localhost:3000> — the dashboard is ready.

Then in your agent:

```python
import synaptic

@synaptic.trace
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

- **Span timeline** — every LLM call, tool invocation, retrieval, and custom span rendered as an indented tree, click any span to expand its input/output JSON
- **Multi-turn sessions** — group related traces under a single conversation; the dashboard's `/sessions` view shows turns in chronological order with aggregate cost, duration, and quality
- **Real-time updates** — WebSocket stream pushes traces and spans into the dashboard the moment they're ingested, no refresh; falls back to 5-second polling if the socket can't connect
- **Cost & token tracking** — automatic per-call breakdown for OpenAI and Anthropic model families
- **Quality scoring** — bring-your-own evals via `POST /v1/evals`
- **Framework integrations** — drop-in support for [LangChain](#langchain) (zero-config via `SYNAPTIC_TRACING=true`), [CrewAI](#crewai) (one-line `instrument_crew()`), and [Anthropic](#anthropic) (`instrument_anthropic()` — captures Claude extended-thinking blocks as first-class child spans)
- **Cross-language SDKs** — Python and TypeScript with identical wire format and behavior
- **Resilient by design** — the agent never fails because Synaptic is down. Spans drop silently if the queue overflows, the exporter is unreachable, or the server returns an error
- **Local-first** — single Docker image, DuckDB + SQLite, no external services, no cloud dependency
- **90-day retention** — automatic cleanup task keeps the database from growing unbounded (configurable via env var)

---

## Architecture

```
   Agent code (Python or TypeScript)
        │  @synaptic.trace / trace(fn)
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
| [`packages/sdk-python/`](packages/sdk-python/) | Python SDK with `@synaptic.trace`, `synaptic.span()`, `synaptic.session()`, integrations | 101 |
| [`packages/sdk-typescript/`](packages/sdk-typescript/) | `@synaptic/sdk` — peer of the Python SDK | 59 |
| [`packages/api/`](packages/api/) | FastAPI ingest + query API, DuckDB storage, WebSocket fanout, session aggregations | 59 |
| [`packages/dashboard/`](packages/dashboard/) | Next.js 14 dashboard with paginated timeline + session view | build + 21 Playwright E2E |

---

## Install

### Python SDK

```bash
pip install -e packages/sdk-python              # core only
pip install -e packages/sdk-python[langchain]   # + LangChain integration
pip install -e packages/sdk-python[crewai]      # + CrewAI integration
pip install -e packages/sdk-python[anthropic]   # + Anthropic integration
pip install -e packages/sdk-python[all]         # all integrations
```

### TypeScript SDK

```bash
# Build the SDK locally (it's not yet published to npm):
cd packages/sdk-typescript && npm install && npm run build

# Then in your project, link or install from the path:
npm install /absolute/path/to/zistica-synaptic/packages/sdk-typescript
```

### Whole stack via Docker

Already covered in [Quick start](#quick-start). One image, both API and dashboard.

---

## Usage

### Python — decorator

```python
import synaptic

synaptic.configure(host="http://localhost:8000")  # or set SYNAPTIC_HOST

@synaptic.trace
def my_agent(input: str) -> str:
    return "hello"

my_agent("test")  # trace appears in the dashboard
```

### Python — context manager (manual span)

```python
with synaptic.span("retrieval", type="retrieval") as s:
    results = vector_db.search(query)
    s.set_output({"count": len(results)})
```

### TypeScript

```typescript
import { configure, trace } from '@synaptic/sdk';

configure({ host: 'http://localhost:8000' });

const myAgent = trace(async (input: string) => {
  return 'hello';
}, { name: 'my_agent' });

await myAgent('test');
```

### LangChain

Zero-config — set the env var before importing LangChain and every chain is auto-traced:

```python
import os
os.environ["SYNAPTIC_HOST"] = "http://localhost:8000"
os.environ["SYNAPTIC_TRACING"] = "true"

from langchain_openai import ChatOpenAI
ChatOpenAI().invoke("Hello")  # span recorded with model, tokens, cost
```

Or attach the handler explicitly:

```python
from synaptic.integrations.langchain import SynapticCallbackHandler

handler = SynapticCallbackHandler()
llm = ChatOpenAI(callbacks=[handler])
```

### CrewAI

```python
from synaptic.integrations.crewai import instrument_crew
instrument_crew()

from crewai import Agent, Task, Crew
crew = Crew(agents=[...], tasks=[...])
crew.kickoff()
# crew.kickoff -> root span
# each agent.execute_task -> child span
# LLM calls inside agents -> grandchildren (via the LangChain integration)
```

### Anthropic

```python
from synaptic.integrations.anthropic import instrument_anthropic
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

### Sessions (multi-turn conversations)

Group several agent calls into one logical conversation:

```python
import synaptic

with synaptic.session(name="booking-conversation"):
    my_agent("Book me a flight to Tokyo")
    my_agent("Make it business class")
    my_agent("Add a hotel for 3 nights")
```

Every traced call inside the `with` block is tagged with a shared `session_id`. The dashboard groups them under `/sessions` with aggregate cost / duration / quality. Click into a session to see the turns in order, click any turn to expand its full span tree.

TypeScript:

```typescript
import { withSession, trace } from '@synaptic/sdk';

const myAgent = trace(async (q: string) => '...', { name: 'agent' });

await withSession({ name: 'booking-conversation' }, async () => {
  await myAgent('Book me a flight');
  await myAgent('Make it business class');
});
```

Or pin a specific session at decoration time:

```python
@synaptic.trace(session_id="user-123-conv-456")
def my_agent(q): ...
```

Resolution priority for a span's `session_id` (most specific wins): explicit `@trace(session_id=…)` > active `synaptic.session()` context > parent span's `session_id` > `None`. Traces without a `session_id` stay in `/traces` as standalone runs and don't appear under any session.

---

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `SYNAPTIC_HOST` | `http://localhost:8000` | API endpoint the SDK posts to |
| `SYNAPTIC_API_KEY` | _(none)_ | Sent as `X-API-Key` header |
| `SYNAPTIC_PROJECT` | `default` | Project identifier |
| `SYNAPTIC_TRACING` | _(unset)_ | Set to `true` to auto-attach the LangChain handler |
| `SYNAPTIC_DATA_DIR` | `./data` (or `/data` in Docker) | Where DuckDB + SQLite files live |
| `SYNAPTIC_RETENTION_DAYS` | `90` | Max age of traces before automatic cleanup |
| `SYNAPTIC_CLEANUP_INTERVAL_HOURS` | `24` | How often the cleanup task sweeps |
| `SYNAPTIC_CLEANUP_ENABLED` | `true` | Set to `false` to disable retention cleanup |

---

## Development

```bash
# Python SDK
cd packages/sdk-python
python -m venv .venv && .venv/bin/pip install -e ".[test,langchain]"
.venv/bin/pytest

# FastAPI
cd packages/api
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest

# TypeScript SDK
cd packages/sdk-typescript
npm install && npm run build && npm test

# Dashboard
cd packages/dashboard
npm install && npm run dev   # http://localhost:3000

# Dashboard E2E (browser tests, requires the Docker container running)
cd packages/dashboard
npm run test:e2e:install     # one-time: download Chromium
npm run test:e2e             # 7 Playwright tests against localhost
```

CI runs all six jobs on every push and pull request — Python SDK, FastAPI, TypeScript SDK, Next.js dashboard build, Docker image build + smoke, and Playwright E2E (real Chromium against the live container). See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## API reference (selected)

```
POST   /v1/spans              # SDKs send here
POST   /v1/traces             # Manual upsert
GET    /v1/traces             # Paginated list (?limit=&offset=)
GET    /v1/traces/{id}        # Single trace
GET    /v1/traces/{id}/spans  # All spans for a trace
GET    /v1/sessions           # Sessions, derived via GROUP BY session_id
GET    /v1/sessions/{id}      # Session detail with all traces in order
POST   /v1/evals              # Submit a quality score
WS     /ws/traces             # Real-time fanout — new_trace + new_span events
GET    /health                # Liveness check
GET    /docs                  # Auto-generated OpenAPI UI
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
- **Contributing** — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are labeled in the [tracker](https://github.com/amitbidlan/zistica-synaptic/issues).
