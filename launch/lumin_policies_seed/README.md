# lumin-policies

Community-contributable starter packs for the **Lumin Agent Firewall**.

> **Status:** Seed content. The public repo at `github.com/amitbidlan/lumin-policies` will mirror this directory. Until then, every pack here is also bundled into the main Lumin distribution at `packages/api/firewall/starter_packs/`.

## What lives here

Each pack is a directory matching spec §13.2:

```
lumin-policies/
├── README.md                          (this file)
├── owasp-llm-top-10-2025/
│   ├── pack.yaml                      metadata (name, version, owner)
│   ├── policies/
│   │   ├── llm01_prompt_injection.yaml
│   │   ├── llm02_sensitive_info.yaml
│   │   └── ...
│   └── README.md
├── owasp-agentic-2025/
├── dev-environment-safety/
├── customer-support-agent/
├── code-assistant/
├── compliance-gdpr/
├── compliance-hipaa/
├── compliance-pci-dss/
├── framework-mastra/
├── framework-langgraph/
└── cost-guards/
```

Each pack ships in **`mode: shadow`** by default. Every Lumin install promotes per the operator's environment.

## Importing a pack into Lumin

The dashboard's `/firewall/library` page lists every available pack and exposes a one-click import. Or via the API:

```bash
# List available packs
curl http://localhost:8000/v1/firewall/library

# Preview a pack before importing
curl http://localhost:8000/v1/firewall/library/compliance_gdpr

# Import — all policies land in mode=shadow, even if the YAML
# accidentally declared otherwise (defense in depth)
curl -X POST http://localhost:8000/v1/firewall/library/compliance_gdpr/import
```

Imports are idempotent — re-running skips duplicate policy names so operator edits never get clobbered.

## Contributing a new pack

Pack format:

```yaml
# pack.yaml
name: my-org-defaults
version: 1
owner: my-org
description: Lumin starter rules for our internal agents.
license: Apache-2.0
```

```yaml
# policies/some_rule.yaml — or a single combined YAML
version: 1
policies:
  - name: my_rule
    description: ...
    lifecycle: before_tool_call
    mode: shadow                 # ALWAYS shadow in starter packs
    priority: 80
    trigger: span_end
    condition: <DSL expression>
    action: block                # block / flag / require_approval / rewrite
    severity: high
```

CI on this repo (when public) runs:

1. **YAML schema validation** — every `condition` parses against the firewall DSL builtins
2. **Fixture replay** — packs are run against a corpus of fixture traces; PRs that introduce new false positives must explain why
3. **Severity sanity** — `block` rules in shadow mode that don't have at least `severity: medium` are flagged for review

## License

Apache 2.0. Same as the Lumin core.

## Ownership

The Lumin team curates this repo. Community PRs welcome — please open an issue first to discuss scope.
