# Lumin launch assets (§17 of AGENT_FIREWALL_SPEC.md)

Marketing + community content shipped alongside the Slice 4 release.

| File / dir | Spec § | Purpose |
|---|---|---|
| `demo_video_script.md` | 17.1 | 3-minute screencast script — covers a Lumin block + a "Block this pattern" promotion + a follow-up enforce. |
| `lumin_policies_seed/` | 17.2 | Seed content for the public `amitbidlan/lumin-policies` repo. Hand off to the new repo with: `git subtree push --prefix=launch/lumin_policies_seed git@github.com:amitbidlan/lumin-policies.git main` |
| `lumin_attack_samples_seed/` | 17.3 | Reproducible adversarial inputs (prompt injection, IPI, jailbreaks, secret-exfil patterns). Generated via the Slice 3 synthetic adversarial generator + curated. |
| `landing_page_competitor_diff.md` | 17.4 | Comparison-table copy for the landing page — Lumin vs. Lakera / NeMo Guardrails / LangChain / Helicone / Phoenix. |
| `blog_posts/` | 17.5 | Four launch blog drafts (markdown). Drop into the company blog or HN-friendly platforms. |
| `clawhub_listing.md` | 17.6 | Copy + metadata for the ClawHub plugin marketplace featured listing. |
| `record_demo.sh` | — | Deterministic recorder: clean Lumin → one `lumin demo` run → `out/lumin-demo.cast` (+ GIF via `agg`). The Show-HN visual, reproducible. |
| `show_hn_post.md` | — | Show HN title options, body, pre-emptive first comment, posting order. Honest about limitations on purpose. |

## How these are meant to be used

- **Demo video script** is a shooting script — read it scene by scene, hit record, no editing besides cuts.
- **Seed repos** are pre-PR content. The repos themselves don't exist yet; I've written the files so they can be `git init`'d into a new repo without further authoring.
- **Blog posts** are first drafts. Each one is ~600 words, hooks-tested, with a clear CTA. Edit before publishing — they're starting points, not final copy.
- **ClawHub listing** is operator-facing copy + JSON metadata for the marketplace entry.

Nothing in this directory ships in the Lumin Docker image. It's all repo-side content for go-to-market.
