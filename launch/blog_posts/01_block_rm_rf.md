# Block `rm -rf` in your OpenClaw agent in five minutes

You've put an LLM in front of a shell. It's reasoning fine. Then on Tuesday it decides the most expedient way to clear a cache is `rm -rf /tmp/cache/*` — and you remember Tuesday was the day you wired in a tool that could actually run that.

This is the easy case. Here's how to stop it in five minutes flat without writing a custom evaluator, without a SaaS subscription, and without the OpenAI Moderation API (which can't see your tool params anyway).

## The setup

You're running OpenClaw with at least one tool that hits a shell, a database, or a filesystem. The OWASP LLM Top 10 has a category — LLM06 *Excessive Agency* — that essentially says "your tool has more privilege than the LLM should have." This is its concrete failure mode.

What you want: the LLM proposes the call, **something between the LLM and the OS asks "are we sure?"** and either auto-blocks based on a rule or pings a human.

## The plugin

```bash
openclaw plugins install @lumin-io/openclaw-diagnostics
docker compose -f https://lumin.dev/docker-compose.yml up -d
```

Two things happened. The OpenClaw plugin started intercepting every tool call and POSTing to a local Lumin API. The Lumin API auto-installed the OWASP LLM Top 10 starter pack — fifteen rules, all in `mode: shadow`. They record what they would have done; they don't actually block anything yet.

Open `http://localhost:3000`. You'll see your traces flowing in real time. Click `/firewall/policies`. Filter for `lifecycle: before_tool_call`. The rule you want is **`owasp_destructive_shell`** — it matches `rm -rf`, `mkfs`, `dd if=`, `drop database`, and a few neighbors.

Send your bot the prompt that triggered the worry: *"please clean up the old cache files."* Watch the Lumin dashboard — there's a new decision row, badge says **🛑 BLOCK (shadow)**. The bot ran the command anyway, because we're in shadow mode. But Lumin saw it.

## Promote to enforce

Click the rule. Hit **"Promote to enforce"**. A modal pops up:

> This rule fired **3 times in the last 30 days** in shadow mode.
> Estimated false-positive rate based on past traffic: **0%**.
> Promote to enforce?

That forecast is the part most security tools skip. You're not flying blind on whether this rule will fire on legitimate traffic — Lumin already replayed the rule against your last 30 days and counted.

Click **Promote**. Now the bot tries `rm -rf` again. It gets back "I can't do that — it requires operator approval."

## The clever bit: now compose

The OWASP rule is a regex. It catches the obvious shapes. Five days from now, the LLM will find a *new* shape: `find /tmp/cache -type f -delete`. That's not on any regex blocklist.

But Lumin saw it happen. Open `/firewall/suggestions`. Lumin has clustered the trace into a pattern alongside other "destructive filesystem operation" traces and proposed a draft rule:

```yaml
- name: ban_destructive_find_delete
  condition: regex_match(...) and is_filesystem_tool(tool_name)
  action: require_approval
```

The forecast says it would have fired 0 times in the last 30 days. You hit **Promote**. Done.

## Why this matters

You didn't write a custom evaluator. You didn't write the YAML. You didn't manually sample traffic to figure out the false-positive rate. The system **observed**, **suggested**, **forecast**, and you only had to say yes.

That's the loop. Lumin's bet is that this loop — observe → suggest → forecast → promote — beats both pure-DSL firewalls (operator has to write every rule) and pure-ML firewalls (no audit trail when something blocks legit traffic).

Five minutes, one shell command, no SaaS account. `github.com/amitbidlan/zistica-lumin`.
