# Your trace from yesterday is the rule you needed today

Most security tools have a wall between observation and enforcement. The observability stack (Phoenix / Helicone / Langfuse) tells you what happened. The enforcement stack (Lakera / NeMo / hand-written guardrails) decides what doesn't happen. They share no schema, no clustering, no feedback loop. When a security engineer notices a bad pattern in observability and wants to block it tomorrow, they have to *manually* re-author it in the enforcement language.

This is the gap Lumin's compounding loop fills.

## The four pieces

**1. Observation.** Every trace and tool call lands in DuckDB. The OWASP starter pack (and any operator rules) ships in `mode: shadow` by default — they record what they *would* have done without actually blocking. Within a day of installation you have a real corpus of "things our agent does that match scary patterns."

**2. Mining.** A frequent-pattern miner runs nightly against the shadow decisions. It clusters by policy, by matched-field shape, by tool call structure. A cluster with > N decisions and high cohesion becomes a candidate suggestion.

**3. Suggestion.** The pattern suggester takes a cluster and emits a draft YAML rule plus a *forecast*: how many traces in the last 30 days would this rule have hit, and what fraction of those hits look like false positives based on the operator's labels table?

**4. Promotion.** Operator opens `/firewall/suggestions`, reviews the draft, sees the FP forecast, and either Promotes (rule lands in shadow → operator validates → operator promotes to enforce) or Dismisses.

That's the loop. The corollary is what makes it feel different from every other firewall: **operators don't write rules. Operators ratify rules.**

## A worked iteration

Day 1: install Lumin. OWASP starter pack auto-loads. Shadow mode on every rule.

Day 7: dashboard shows ~200 shadow decisions. Most are noise, but 8 of them are clustered around `is_shell_tool(tool_name) and regex_match(command, "(?i)find\\s.*-delete")`. The miner notices.

Day 8: a suggestion appears in `/firewall/suggestions`:

```
ban_destructive_find_delete
Cluster size: 8 decisions
Forecast (last 30d): 8 hits, 0 in operator-labeled-as-good
Confidence: 0.94
[Preview] [Promote to shadow] [Dismiss]
```

You preview. The rule looks right. You promote. It now lives as a real shadow policy with `priority: 80`.

Day 14: the new shadow rule has fired 3 more times. None of them landed in your operator-labeled-as-good. You promote it to `enforce`.

Day 15: a new attack arrives. It uses `xargs rm` instead of `find -delete`. The operator labels the trace as `bad`. The miner picks it up overnight. Day 16, a new suggestion appears.

This is the compounding part. **Each labeled trace makes the next rule more likely to be the right rule.** The longer Lumin runs against your traffic, the better-fitted your firewall is to *your* attack surface — not a generic blocklist tuned for everyone.

## Why other tools can't do this

The closest mechanism is AWS WAF Managed Rules — periodically updated rules from the vendor. But those are universal: every customer gets the same blocklist. Lumin's miner runs *on the operator's own corpus*, so the rules are operator-specific.

NeMo Guardrails has an authoring DSL but no traffic-mining loop. The operator writes every rule by hand.

Lakera has ML detection but isn't OSS, so the operator's traffic doesn't train per-deployment models — it trains Lakera's vendor model, shared across every customer.

Phoenix / Langfuse have observability and labeling but no enforcement engine, so labels can't become rules.

The compounding loop only works when **observation, labeling, mining, suggestion, forecast, promotion, and enforcement live in one box** — sharing a schema, a database, a UI, and a security boundary. That's what Lumin is.

## What's coming

The miner currently clusters by matched-field similarity. The next iteration uses operator labels (`bad` / `good` / `neutral`) as features — a `bad`-labeled cluster gets a higher promotion priority, a `good`-labeled cluster gets surfaced as a *false-positive warning* against existing rules.

The local fine-tuned classifier (Slice 3 §6.8) is the deepest version of the loop: each operator's labels train an ONNX classifier unique to their deployment. The longer it runs, the more your firewall recognizes *your* attack patterns even when they don't match any rule.

Today, install Lumin in shadow mode. In a week, you'll have your first real enforcement rule that you didn't write. In a month, you'll trust it more than the generic OWASP rules you started with.

`github.com/amitbidlan/zistica-lumin` — Apache 2.0, local-first, sixty-second install.
