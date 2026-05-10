# Changelog

All notable changes to Lumin are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [0.7.0] — 2026-05-11

### Added — Tenant-isolation firewall (operator UX)
- One-knob `securityProfile` config: `strict` / `standard` / `light` / `logging-only` (legacy aliases preserved). Each sets sensible defaults across all five layers.
- Six plain-English toggles to override profile defaults: `enableTenantIsolation`, `blockShellTools`, `blockWebTools`, `resetMemoryBetweenUsers`, `hideOtherUsersData`, `recordSecurityEvents`.
- Dashboard page at `/settings/firewall` — profile picker (4 cards), per-toggle override controls, effective-settings preview panel, recent-activity stat row, sticky save bar with reset-overrides + `logging-only` confirmation dialog.
- Server-side `firewall_settings` DuckDB table + `GET/PUT /v1/admin/firewall/profile` endpoint (gated by `LUMIN_API_TOKEN` middleware). Plugin polls every 30s and merges over `openclaw.json`.
- Per-detector toggles on `/v1/firewall/redact-context` (`vault_exact` / `structural_pattern` / `presidio`).
- Plugin manifest auto-sync via `npm run build` (`scripts/sync-extension.mjs`) — keeps the installed extension dir aligned with source dist + manifest.
- Per-agent firewall config — `ensureAgentSettings(ctx?.agentId)` re-fetches when the active agent changes; falls back to `_default` row.

### Added — Tenant-isolation firewall (defense layers)
- L1.5 deny-by-default for shell/code-exec tools (`exec`, `shell`, `bash`, `python`, `node`, `ruby`, …) AND network-egress tools (`web_fetch`, `http_get`, `http_post`, `http_put`, `http_delete`, `fetch`, `curl`).
- L1 storage sandbox extended to cover `grep`, `find`, `cat`, `head`, `tail` (in addition to `read` / `edit` / `write` / `search` / `ls`).
- L1 `sharedPaths` allowlist for cross-sender read-only files. Writes to shared paths are blocked.
- L1 fail-closed mode (`failClosedOnMissingWorkspace`) — refuses fs tool calls when workspace context can't be resolved.
- L2 conversation-history reset modes: `clear-on-switch` (default), `scope-by-channel`, `off`.
- L3 input redactor: Microsoft Presidio NER installed in the Docker image (default `en_core_web_lg`, swappable to `_md`/`_sm` via `LUMIN_PRESIDIO_MODEL` build arg). Catches PERSON, LOCATION, EMAIL_ADDRESS, PHONE_NUMBER, US_SSN, CREDIT_CARD, IP_ADDRESS, US_PASSPORT, US_DRIVER_LICENSE + custom suffix-bearing ORGANIZATION recognizer (Inc / Corp / LLC / Logistics / Health / Capital / Industries / …).
- L4 audit row sampling (`auditSamplingRate` 0.0–1.0) for high-volume deployments. Plugin-side blocks now generate `policy_violations` rows via `LuminClient.sendViolation()` — previously these lived only in plugin stdout.

### Added — Demo + content
- 52-second demo video at `assets/demo.mp4` (2.4MB H.264) showing a cross-session leak attempt and Lumin's five-layer defense in action.
- README rewritten Langfuse-style: hero banner, centered nav + badges, bold-keyword tagline, comparison table (vs Langfuse / Lakera / NemoGuard), numbered emoji quickstart, integrations table, packages table.
- 17 GitHub topics applied (ai, observability, llm, langchain, opentelemetry, developer-tools, local-first, tracing, agents, crewai, agent-firewall, tenant-isolation, prompt-injection, mastra, voltagent, openclaw, duckdb).

### Fixed
- L3 redactor silently dropped every Presidio detection because `presidio_pii_entities()` returned `{entity_type, score, start, end}` but the consumer in `vault._extract_facts` reads `text`. Now populates `text = original_text[start:end]`.
- Custom Presidio ORG recognizer no longer matches lowercase prose (Presidio's `PatternRecognizer` hardcodes `re.IGNORECASE`; switched to `(?-i:…)` inline flag for the case-sensitive prefix-word group).
- Plugin's `before_tool_call` / `before_prompt_build` await the initial dashboard merge with a 1s hard timeout, closing the race where the first hook ran on stale `openclaw.json` defaults.
- Dashboard span detail (light theme): `text-violet-100` reasoning content was invisible on `bg-white`; span type-badge pills had similar pale-on-pale issues. Migrated to theme-aware `--thinking-*` and `--span-*` CSS vars that flip per `data-theme`.

### Tests
- 23 vitest cases for `resolveSecuritySettings`. 8 pytest cases for `/v1/admin/firewall/profile`. 34 sandbox + 10 input-redactor cases still green. Total 67 plugin + 8 admin tests passing.

### Added
- Initial Python SDK with `@lumin.trace` decorator
- FastAPI ingest and query API
- DuckDB local storage (traces, spans, evals)
- Next.js dashboard with span timeline
- Single Docker image: `docker run -p 3000:3000 zistica/lumin`
- LangChain integration
- CrewAI integration
- TypeScript SDK
- Anthropic integration with extended-thinking visualization — `instrument_anthropic()` wraps `Messages.create` (and the streaming `Messages.stream` context manager) and emits a `claude_call` parent span with `thinking` and `response` children. Available in both the Python SDK (`lumin.integrations.anthropic`) and the TypeScript SDK (`@lumin-io/sdk/integrations/anthropic`) with identical wire format. Dashboard renders thinking rows with a brain emoji, violet highlight, and per-trace thinking-vs-response cost breakdown. New `span_subtype` and `thinking_tokens` columns on the spans table (auto-migrated for legacy DBs)
- LlamaIndex integration — `LuminCallbackHandler` plugs into `Settings.callback_manager` and ships every LLM call, retrieval step, embedding, and query as a Lumin span. Captures model/tokens/cost on LLM events and node count + similarity scores on retrievals. Install via `pip install lumin[llama_index]`

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

[Unreleased]: https://github.com/amitbidlan/zistica-lumin/compare/HEAD
