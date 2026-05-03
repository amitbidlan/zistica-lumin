# Changelog

All notable changes to Synaptic are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- Initial Python SDK with `@synaptic.trace` decorator
- FastAPI ingest and query API
- DuckDB local storage (traces, spans, evals)
- Next.js dashboard with span timeline
- Single Docker image: `docker run -p 3000:3000 zistica/synaptic`
- LangChain integration
- CrewAI integration
- TypeScript SDK
- Anthropic integration with extended-thinking visualization — `instrument_anthropic()` wraps `Messages.create` (and the streaming `Messages.stream` context manager) and emits a `claude_call` parent span with `thinking` and `response` children. Available in both the Python SDK (`synaptic.integrations.anthropic`) and the TypeScript SDK (`@synaptic/sdk/integrations/anthropic`) with identical wire format. Dashboard renders thinking rows with a brain emoji, violet highlight, and per-trace thinking-vs-response cost breakdown. New `span_subtype` and `thinking_tokens` columns on the spans table (auto-migrated for legacy DBs)
- LlamaIndex integration — `SynapticCallbackHandler` plugs into `Settings.callback_manager` and ships every LLM call, retrieval step, embedding, and query as a Synaptic span. Captures model/tokens/cost on LLM events and node count + similarity scores on retrievals. Install via `pip install synaptic[llama_index]`

### Fixed
- Anthropic integration: `thinking` and `response` child spans were created with `started_at = now()` after `parent.end()` ran, so each child's interval fell strictly after the parent's. Children now inherit the parent's bracket and split it 80/20 (thinking-then-response, sequential, non-overlapping)
- API: `trace.total_cost_usd` and `trace.total_tokens` now aggregate from child spans on read. Previously a trace ingested via `POST /v1/spans` (the SDK path) always reported $0 / 0 even when its children had real cost and token data. `GET /v1/traces` and `GET /v1/traces/{id}` use `GREATEST(stored, sum-from-spans)` so explicit `POST /v1/traces` totals still win when larger

---

## How to Read This

Each release has sections for:
- **Added** — new features
- **Changed** — changes to existing behavior
- **Fixed** — bug fixes
- **Removed** — removed features
- **Security** — security fixes (always upgrade immediately)

[Unreleased]: https://github.com/zistica/synaptic/compare/HEAD
