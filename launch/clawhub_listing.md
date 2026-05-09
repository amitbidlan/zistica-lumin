# ClawHub featured listing

Copy + metadata for the **`@lumin-io/openclaw-diagnostics`** listing on `clawhub.ai/plugins`. We're pushing for the Featured slot — coordinate with OpenClaw maintainers via @steipete.

## Plugin metadata (clawhub.json fields)

```json
{
  "name": "@lumin-io/openclaw-diagnostics",
  "displayName": "Lumin Agent Firewall",
  "category": "security",
  "tags": [
    "firewall",
    "security",
    "observability",
    "policy",
    "owasp-llm-top-10",
    "compliance",
    "self-hosted",
    "apache-2-0"
  ],
  "license": "Apache-2.0",
  "homepage": "https://github.com/amitbidlan/zistica-lumin",
  "documentation": "https://github.com/amitbidlan/zistica-lumin/tree/main/packages/integrations/openclaw-diagnostics#readme",
  "issues": "https://github.com/amitbidlan/zistica-lumin/issues",
  "compatibleWith": ">=2026.5.0",
  "minNodeVersion": "20.0.0"
}
```

## One-line tagline

> **Real-time policy enforcement + observability for OpenClaw agents. OWASP LLM Top 10 + per-deployment learned classifier. Apache 2.0. Local-first.**

## Short description (160 char limit, used in card view)

> Block prompt injection, PII leaks, destructive tool calls, and indirect injection in real time. 11 starter packs. Per-deployment classifier.

## Long description (full listing page)

### What it does

Lumin sits between OpenClaw and the LLM provider, gating every tool call and every proxy turn against policy. When the firewall blocks, the user sees Lumin's canned message — the LLM is bypassed entirely.

### What you get out of the box

- **Synchronous enforcement** at 5 lifecycle hooks (`before_proxy_call`, `after_proxy_call`, `before_tool_call`, `after_tool_call`, `post_ingest`)
- **8 OpenClaw hooks subscribed**, including the `before_dispatch` reply takeover — proven on Telegram in production
- **OWASP LLM Top 10** auto-installed in shadow mode
- **11 starter packs** — OWASP Agentic AI, dev-environment, customer support, code assistant, GDPR / HIPAA / PCI DSS, framework-Mastra, framework-LangGraph, cost-guards
- **Detectors out of the box** — regex pack, Microsoft Presidio, Prompt Guard 2, Llama Guard 4, embedding similarity, IPI sniffer, LLM-as-judge, behavioral anomaly, drift, frequent-pattern miner, local fine-tuned classifier
- **Compounding rules** — observed traces become suggested rules with false-positive forecast
- **Five-verb decision contract** — allow / block / flag / require_approval / rewrite
- **Reply takeover** — single canned message reaches the user, no double-message confusion
- **Approvals inbox** — operator approves destructive tool calls in real time, agent waits

### Quick start

```bash
# 1. Install the OpenClaw plugin
openclaw plugins install @lumin-io/openclaw-diagnostics

# 2. Start the Lumin API + dashboard locally
docker compose -f https://lumin.dev/docker-compose.yml up -d

# 3. Open the dashboard
open http://localhost:3000
```

The plugin auto-detects Lumin at `localhost:8000` and starts streaming traces. Block-class decisions also fire to your configured Slack / Discord / PagerDuty / generic webhook.

### Configuration

The plugin reads its config from `openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "@lumin-io/openclaw-diagnostics": {
        "enabled": true,
        "config": {
          "host": "http://localhost:8000",
          "project": "openclaw",
          "enforce": true,
          "onFirewallError": "allow",
          "userInputBlockedMessage": "Your message could not be processed due to security policy."
        }
      }
    }
  }
}
```

### Licensing

Apache 2.0 (the same as OpenClaw itself). No paywalled features. The Lumin server is also Apache 2.0; Phase 3 enterprise features (signed packs, multi-tenant) are planned but not gating today's functionality.

### Maintenance

Active. Updates land within a week of OpenClaw minor releases. File issues / PRs at `github.com/amitbidlan/zistica-lumin`.

## Featured-slot pitch (for the OpenClaw maintainers)

We're requesting the Featured slot for the security category because:

1. **No other security plugin in ClawHub provides full agent-firewall enforcement** — the closest are observability plugins (`@openclaw/diagnostics-otel`, etc.) which don't gate execution.
2. **It's deeply OpenClaw-native** — uses the typed-hook surface end-to-end, not a generic adapter. Subscribes to 8 hooks. Discovered and proved out the `before_dispatch` reply-takeover pattern through dogfood, which we've documented for the OpenClaw community.
3. **Apache 2.0** — there's no upsell to a paid tier blocking ClawHub users.
4. **Cross-promotion** — every Lumin install pulls in OpenClaw users from the Python / LangChain / LangGraph ecosystem who otherwise wouldn't know about ClawHub.

Happy to coordinate a joint launch post + ClawHub blog feature with the OpenClaw team.
