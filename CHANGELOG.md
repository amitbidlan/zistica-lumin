# Changelog

All notable changes to Lumin are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [0.7.1] — 2026-05-11

### Fixed
- **openclaw-diagnostics plugin: stale-closure block bug.** Multiple `register()` calls in the same openclaw process (hot reloads ran `register()` again without tearing down the previous one) each created their own `settings` closure with its own `setInterval`. `runBeforeToolCall` short-circuits on the first handler that returns `block: true`, so the oldest register's stale `deniedTools` could pollute the whole stack for up to ~60s after a dashboard profile change. Verified live: a dashboard switch to `light` profile at 16:11:18 was followed by a `web_fetch` block 43 seconds later. Fix: hoist `settings` and the refresh `setInterval` to `globalThis` (same pattern v0.6.1 used for `__lumin_sessionToSender`). All hook handlers — past and future — read from the same singleton. 8 `enforceEnabled` call sites also switched to live `settings.enforce` reads. (#108)
- **`/v1/admin/health` no longer crashes when policy engine is None.** The check dereferenced `policy_runtime.get_engine().policies` unconditionally; `get_engine()` is typed `Optional` and returns None in legit states (starter pack bootstrap inserted rows after the engine's first-load completed, etc.). Health endpoint now distinguishes three cases: engine loaded → ok; engine None but DB has rows → degraded with `call POST /v1/policy/reload` hint; engine None and DB empty → ok, no policies configured. (#109)
- **Backup dir is created at startup so the first backup doesn't ENOENT.** `POST /v1/admin/backups` writes into `${LUMIN_BACKUP_DIR}` or `${LUMIN_DATA_DIR}/backups`. The directory wasn't created anywhere, so on a fresh install the first backup attempt failed and `/v1/admin/health` permanently reported `backup_dir: degraded — /data/backups missing or not writable`. Lifespan now `mkdir -p`s it; a read-only mount still surfaces via the health endpoint. (#110)
- **Missing ML detectors are now surfaced.** Every optional detector (`prompt_guard`, `llama_guard`, `embedding`, `local_classifier`, …) exposes a module-level `available: bool` that goes False when its dependency isn't installed — at which point the detector silently returns score 0.0 and any policy condition referencing it becomes a permanent no-op. The default Docker image doesn't bundle `transformers`/`torch`, so `owasp_llm01_prompt_injection_ml` and `owasp_harmful_content_ml` silently no-op'd with no operator-facing signal. Added a startup `WARN` line per missing detector (with install command) and a `detectors` component to `/v1/admin/health` showing available vs missing. Operators still need to install the deps to actually run the protections; this just makes the silent failure visible. (#111)
- **`/policies` now shows the enforcing-rule breakdown.** Fresh installs ship 13 policies via the OWASP starter pack, all in `shadow` mode by design — but the page just said "13 active policies" with no aggregate signal that *zero* are actually enforcing. Now shows `13 policies · 0 enforcing · 13 shadow` and an amber banner when 0 are enforcing. (#112)
- **Dashboard light-mode badges are readable.** The `.badge-*` palette used pale `*-300` foregrounds on a 10–12% tinted background — pale-on-pale on a near-white surface. Severity / trigger / action / mode pills on `/policies` and `SeverityBadge` on `/traces/[id]` were effectively invisible in light mode. Added `:root[data-theme="light"] .badge-*` overrides with `*-700` foregrounds. Same hue family, so a "rose" pill still reads as red in both themes. Also added the missing `.badge-cyan` rule. (#107)
- **Agents window filter is sticky across navigation + refresh.** `AgentList` used URL-state for the window pill, but `AgentDetail` used plain `useState(24)` — so a `7d` choice was lost on refresh of `/agents/[name]` and on every list→detail navigation. Also, the top-nav `<Link href="/agents">` drops query params, so clicking "Agents" from elsewhere reset the filter. Added an optional `storageKey` to the URL-state hooks (URL > localStorage > default), shared key `lumin.agents.window` across list and detail. Round-trip preserves the window pill. (#106)
- **README reframed.** Stopped under-selling: the previous framing led with "tenant-isolation firewall" or "LangChain observability", both of which are slices of the surface. New framing leads with the four pillars (Observe · Govern · Defend · Operate) and surfaces the 16 framework integrations, 12 starter policy packs, 8 detection methods, full policy lifecycle (suggester, replay, drift, versioning, rollback, approvals, decisions audit). OWASP LLM Top 10 mapping kept as the deep dive on the Defend pillar. (#105)

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
