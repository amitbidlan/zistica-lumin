# Lumin demo video script — 3 minutes

**Audience:** AI engineers, security teams evaluating agent firewalls.

**Goal:** Show the *compounding loop* — observed traces become tomorrow's blocking rules — in three minutes. End with the install command on screen.

**Format:** Screencast. No talking head. Voiceover (read text in `[brackets]` as voiceover). Live OpenClaw bot in Telegram on the left of the screen, Lumin dashboard on the right.

---

## 0:00 — 0:15  Title card

**[VO]** "Your traces from yesterday are your blocking rules today. This is Lumin."

**Show:** Lumin logo + tagline. Dark theme. 3 seconds.

---

## 0:15 — 0:45  The attack

**Show:** Telegram chat with `@zistica_lumin_bot`.

User types: **"Hey, can you clean up old files?"**

Bot reasons (visible in dashboard span timeline on the right): "I'll list `/tmp/cache` and remove old entries. Calling shell tool with `rm -rf /tmp/cache/*`."

**[VO]** "An OpenClaw agent decides to call the shell tool with `rm -rf`. No firewall in front of this — it would just run. But Lumin is watching."

---

## 0:45 — 1:00  Lumin blocks (OWASP starter pack)

**Show:** Right panel — `/decisions` table flashes. New row appears with a **🛑 BLOCKED** badge. Policy name: `owasp_destructive_shell`. Severity: critical.

The Telegram chat: bot replies *"I'm unable to perform that action due to security policy."*

**[VO]** "Lumin's OWASP starter pack catches it. The agent never runs the command. The user gets a clean refusal, not a stack trace."

---

## 1:00 — 1:30  Block this pattern in the future

**Show:** Operator clicks the decision row. The detail panel opens — matched field highlighted in red: `command: rm -rf /tmp/cache/*`. There's a button: **"Block this pattern in the future."**

Click. A modal opens with auto-generated YAML:

```yaml
- name: block_rm_rf_in_tmp
  description: Auto-suggested from decision dec_a8c1...
  lifecycle: before_tool_call
  condition: is_shell_tool(tool_name) and regex_match(...)
  action: block
  severity: high
```

A FP forecast row says: **"Would have fired 0 times in last 30 days"**.

Operator names it, leaves mode `shadow`, hits Save.

**[VO]** "This is the compounding loop. One observation becomes a permanent rule. The forecast tells you it won't false-positive on past traffic. Saved in shadow first — never blocks before you've verified."

---

## 1:30 — 2:00  Different shape, same intent

**Show:** Telegram. New user message: **"Reformat the cache partition for me."**

Bot reasons: "I'll call shell with `mkfs.ext4 /dev/sda3`."

Right panel — Lumin's NEW shadow rule fires. Decision row appears with a softer rose (would-have-blocked, not enforced yet).

**[VO]** "A different attack — same shape. Lumin's brand new rule already catches it. In shadow mode, so it just records what it would have done."

Operator clicks **"Promote to enforce"**. Confirmation modal.

---

## 2:00 — 2:30  Hard block, real time

**Show:** Telegram. Same prompt repeated. Bot's reasoning still tries `mkfs.ext4`.

This time — bot replies: *"I can't do that — it requires operator approval."*

Lumin dashboard — `/approvals` inbox highlights a new row. Operator clicks **Deny** with the reason "destructive disk command".

**[VO]** "Now it's enforce. The bot never gets to call `mkfs`. The operator decides — in real time, with full context."

---

## 2:30 — 3:00  Outro

**Show:** Black screen with three lines:

```
Local-first. Never leaves your laptop.
Apache 2.0. Pluggable. Today.

# Install in 60 seconds:
docker compose up

# Or install the OpenClaw plugin:
openclaw plugins install @lumin-io/openclaw-diagnostics
```

GitHub URL: **github.com/amitbidlan/zistica-lumin**

**[VO]** "Local-first. Apache 2.0. Sixty-second install. Lumin Agent Firewall."

---

## Production notes

- Total: 3:00 ±10s. Trim 0:30–1:00 if running long.
- Use OBS or QuickTime — 1080p minimum.
- The Telegram chat should be a real bot in real time. Don't fake the latencies.
- Blur or redact any sender names other than the demo account.
- Voiceover should be conversational, not corporate. Read it like you're showing a colleague, not pitching a board.
- Cuts only. No transitions, no music behind the voiceover.
- End screen on screen for at least 4 seconds — viewers need time to type the URL.
