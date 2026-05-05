# Lumin — framework integration status

A living tracker of which AI agent frameworks Lumin supports today, which are queued, and the rough effort cost for each. Top of file is current state; bottom is the roadmap.

---

## Shipping today

| framework | language | how to install | published as | first release |
|---|---|---|---|---|
| **OpenClaw** | TS | `npm i @lumin-io/openclaw` | npm package | `0.1.0` |
| **Mastra** | TS | `npm i @lumin-io/mastra` | npm package | `0.1.0` |
| **VoltAgent** | TS | `npm i @lumin-io/voltagent` | npm package | `0.1.0` |
| **LangChain** | Python | `pip install lumin` | bundled in Python SDK (`lumin.integrations.langchain`) | `0.1.0` |
| **LlamaIndex** | Python | `pip install lumin` | bundled in Python SDK (`lumin.integrations.llama_index`) | `0.1.0` |
| **CrewAI** | Python | `pip install lumin` | bundled in Python SDK (`lumin.integrations.crewai`) | `0.1.0` |
| **Anthropic SDK** (raw) | Python + TS | `pip install lumin` / `npm i @lumin/sdk` | bundled in both SDKs | `0.1.0` |

### Effectively covered, no separate package needed

| framework | how it works today |
|---|---|
| **Vercel AI SDK** | `@lumin-io/mastra` reads the same `ai.prompt.messages` / `ai.response.text` / `ai.response.reasoning` / `ai.toolCall.*` keys the Vercel AI SDK emits. Drop-in for any Vercel-AI app — just rename the import. (We may ship a thin `@lumin-io/vercel-ai` re-export package for nicer DX.) |

---

## Roadmap — frameworks queued

Sorted by effort. Each row's effort assumes the OTel-bridge pattern we use for the existing TS integrations works (otherwise see the "shape" column for the alternative).

### Tier 1 — OTel-native, ~half-day each

| framework | language | shape | priority signal |
|---|---|---|---|
| **OpenAI Agents** | Python (+ TS) | Custom `TracingProcessor` (their native protocol), with OTel as fallback | Highest brand visibility on the list — official OpenAI SDK |
| **Google ADK** | Python (+ Java) | OTel SDK ride-along; doc page + smoke test | Attaches Lumin to the Google ecosystem |
| **Pydantic AI** | Python | Logfire → OTel; thin Python integration or doc-only path | Pydantic / FastAPI overlap with our existing user base |

### Tier 2 — OTel-supported but mixed telemetry, ~1 day each

| framework | language | shape |
|---|---|---|
| **Semantic Kernel** | Python + .NET | Python integration first; defer .NET (nuget package or rely on OTel collector) |
| **Haystack** | Python | Pipeline-step callback adapter (haystack-experimental has OTel) |

### Tier 3 — custom callback integration, ~1–2 days each

| framework | language | shape |
|---|---|---|
| **AutoGen** | Python | Microsoft. v0.4+ adding OTel; partial coverage. LangChain-style callback integration. |
| **SmolAgents** | Python | HuggingFace. Step-callback integration; less standardized. |

---

## Recommended next pickup order

Tradeoff: traffic × effort.

1. **OpenAI Agents** — 1 day, but biggest discoverability ceiling on the list (official OpenAI SDK).
2. **VoltAgent** — ✅ shipped 2026-05-04
3. **Google ADK** — half-day, attaches to Google ecosystem.
4. **Pydantic AI** — half-day, FastAPI/Pydantic overlap with our users.
5. **Then Tier 2/3** — order by inbound demand from Discord / awesome-list / GitHub stars.

---

## Surfaces & supporting infra

| surface | URL | status |
|---|---|---|
| GitHub repo | <https://github.com/amitbidlan/zistica-lumin> | live |
| GitHub release | <https://github.com/amitbidlan/zistica-lumin/releases/tag/v0.1.0> | live |
| Docker image | <https://hub.docker.com/r/zistica/lumin> | live (multi-arch: linux/amd64 + linux/arm64) |
| ClawHub skill | `clawhub install lumin` | live |
| awesome-openclaw-agents PR | <https://github.com/mergisi/awesome-openclaw-agents/pull/61> | open, awaiting maintainer |
| OpenClaw Discord post | `#clawhub` on <https://discord.gg/clawd> | posted |

---

## Test coverage (across all six packages)

| package | tests |
|---|---:|
| sdk-python | 172 |
| api | 65 |
| sdk-typescript | 59 |
| integrations/mastra | 55 |
| integrations/openclaw | 59 |
| integrations/voltagent | 62 |
| **total** | **472** |

---

## Per-integration brutal-test protocol (used for VoltAgent, will reuse)

Before merging any new integration PR:

1. Issue first — file a tracking issue with scope, plan, success criteria.
2. Implement + tests.
3. Wait for CI green (8/8 jobs).
4. **5 brutal-test rounds** — each round runs a complex multi-trace demo, audits the data layer, and visually checks the dashboard at `localhost:3000` for: span tree intact, costs computed, tool spans classified correctly, input/output content visible, errors flagged red, sessions group correctly, WebSocket live-updates streaming.
5. **Only after 5/5 rounds pass do we merge + publish.**

VoltAgent's round 1 surfaced two real bugs (guardrail input/output keys + sessions aggregation broken for ALL frameworks); both were fixed before merge. This protocol is now the bar for every future integration.
