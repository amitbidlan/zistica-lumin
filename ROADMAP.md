# Lumin Roadmap

One screen. Updated as things ship. This is the public source of truth —
if it's not here, it's not promised.

> **What Lumin is:** see everything your AI agent does, and stop it before it
> does something catastrophic. 100% local, one command, Apache-2.0.

---

## 🔜 Now — pre-Show-HN blockers

The bar before we post anywhere. These gate the launch, in order:

- [ ] **Distribution name.** `pip install lumin` resolves to an unrelated PyPI
      project. Decide and ship one of: a vanity name (`lumin-ai`),
      `pipx`/`uvx run`, or a one-line installer script. *Frictionless install
      is the #1 conversion lever — this blocks it.*
- [x] **`lumin` CLI** — `lumin up` / `lumin demo` / `lumin doctor`. The demo
      fires a real attack at the live firewall and shows real blocks. No keys.
- [ ] **One-thing demo GIF (≤15s)** — attack fired → red BLOCK row → trace
      shows the catch. Distinct from the existing 52s walkthrough.
- [ ] **Hosted try-it dashboard** — seeded read-only instance so a visitor
      sees the product before installing anything.
- [ ] **15+ triaged `good-first-issue`s** — mostly framework integrations
      (see the *New framework integration* issue template).

## 🏗️ Next — the wedge

- [x] **Agent Security Scorecard** — `lumin scorecard` grades firewall
      coverage (enforced vs potential) **and** `--target <url>` grades a real
      external OpenAI-compatible agent (delivers the OWASP attack suite,
      judges replies with Lumin's output detectors → "AI agent OWASP safety
      X%"). Writes shareable `SCORECARD.md` + shields badge; `--json` for CI.
      Verified live end-to-end. *Next: a hosted gallery of public scores.*
- [ ] **Zero-switch OTel/Langfuse compat, marketed** — the OTLP receiver +
      proxy already exist; ship a one-page "keep your stack, gain the
      firewall" migration guide.
- [ ] **Reproducible public benchmark** — Lumin vs. a named prompt-injection
      corpus, numbers + methodology in `/bench`.
- [ ] **Dashboard auth, on by default for non-localhost binds** — the
      mechanism exists (`LUMIN_DASHBOARD_PASSWORD`); make safe the default.

## 🌅 Later — the moat

- [ ] Integration ubiquity: be the default observability snippet in every
      major framework's docs (the Sentry playbook).
- [ ] Compounding per-deployment classifier — improves on *your* traffic,
      inside *your* box. A cloud vendor structurally can't copy this.
- [ ] Fleet control plane (the open-core paid line — manage many local
      Lumins; the data plane always stays self-hosted and free).

---

## Contribute (10 minutes to your first PR)

1. `git clone` → `./setup.sh` → `cd packages/api && pytest` (should pass).
2. Pick a [`good-first-issue`](https://github.com/amitbidlan/zistica-lumin/issues?q=is%3Aissue+is%3Aopen+label%3Agood-first-issue).
   **Framework integrations are the best starter** — self-contained, copy an
   existing one in `packages/sdk-python/lumin/integrations/`, add a test.
3. Open a PR. Maintainers respond to `good-first-issue` threads within 24h —
   we will not let your PR rot.

The contributor funnel is deliberately the integration surface: each new
framework is one file + one test + one README row, with a working template
to copy. See `.github/ISSUE_TEMPLATE/integration_request.yml`.
