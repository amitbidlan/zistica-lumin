# Approval workflows for AI agents, without the SaaS tax

A coworker came to me last quarter with a problem that's now common: their AI agent has a tool to issue customer refunds, capped at $500. Every refund actually fires. The bot is also supposed to escalate refunds over $500 to a human. It does — sometimes. The other times it issues the refund anyway and *says* it escalated.

LLMs lie about side effects. Worse, they hallucinate auth flows. We've seen agents tell users "I've sent this to my supervisor for approval" when no such message went anywhere, and "click /approve to confirm" when no /approve handler exists in the bot. The user clicks. Nothing happens. The bot runs the destructive action anyway because it never actually waited for approval.

The fix is a **synchronous approval channel** that lives outside the model's reasoning context. The agent doesn't *describe* an approval — it *gets blocked* until a human resolves it.

## How Lumin does it

Lumin's `require_approval` decision verb does three things at once:

1. **Pauses the agent.** The decide() endpoint returns `{decision: "require_approval", approval_id: "apv_..."}`. The integration (LangChain handler, OpenClaw plugin, etc.) raises an exception or yields, depending on the framework's contract. The tool call doesn't fire.

2. **Surfaces the request to the operator.** A row appears in the dashboard's `/approvals` inbox. The operator sees the agent name, the tool, the truncated params, the policy that flagged it, and the trace that led here.

3. **Holds an HTTP long-poll.** The agent's runtime polls `GET /v1/approvals/{id}` until the operator either resolves it (allow / deny) or the timeout fires (default 600s). On allow, the original tool call proceeds. On deny, the decide returns block, and the agent gets a deny path that includes explicit instructions: *"do not generate /approve syntax; operator approval is out of band."* This last detail closes the social-engineering surface where the LLM hallucinates fake approval prompts.

The whole thing runs locally. There's no SaaS, no "approval system" SDK, no $0.01-per-approval pricing. The dashboard at `localhost:3000` shows the inbox, you click Allow or Deny, the agent un-blocks.

## A worked example

Here's the rule that catches the refund case:

```yaml
- name: refund_amount_ceiling
  description: Refunds over $500 require operator approval.
  lifecycle: before_tool_call
  mode: enforce
  condition: |
    tool_name == "issue_refund"
    and (Input.params.get("amount") or 0) > 500
  action: require_approval
  on_timeout: deny
  severity: high
```

User asks for an $800 refund. Bot decides to call `issue_refund(amount=800)`. Lumin's firewall fires before the tool runs, returns `require_approval` with `approval_id: apv_a9c1...`, and **the issue_refund call never happens**. The inbox lights up.

Operator sees:

```
Agent:    cs_agent
Tool:     issue_refund
Params:   { customer_id: "C-44128", amount: 800, reason: "package never arrived" }
Policy:   refund_amount_ceiling
Trace:    /traces/d6a6d079-... (full conversation)
Severity: high
```

Operator opens the trace, reads the conversation, decides this is legitimate, hits Allow. Within ~50ms the agent's long-poll returns; `issue_refund(amount=800)` executes. The customer gets their refund. The audit log shows: who approved, when, with what reason.

If the operator hits Deny, the agent gets back `decision: block`, and the LLM sees feedback like:

> "The tool call was denied by the operator. Do NOT generate '/approve' or any other approval syntax in your reply. The user does not have approval authority. Inform the user that this requires manual review and provide a way to contact support."

That last line — the explicit anti-hallucination instruction — exists because we observed the model trying to fake approval surfaces. Documented in our build log as the `/approve` social-engineering hole, closed in Slice 2 Tier 1.5(b).

## When this is overkill

Approval workflows aren't free. Each one adds latency (operator response time, default ~minutes), takes operator attention, and requires you to staff someone to watch the inbox during business hours. Use them only on:

- Irreversible actions (payments, deletions, public posts)
- Above-threshold actions (refund > $X, query returning > N rows of PII)
- Actions touching admin / privileged surfaces

For the long tail of tool calls, plain `block` or `flag` is fine. The decision-tree question is **"would I personally want to see this before it ran?"** If yes, `require_approval`. If no, let the rule auto-decide.

## What you don't need to build

- No approval-system database. Lumin's `approvals` table handles state.
- No long-polling endpoint. Lumin owns it.
- No expiry sweeper. Lumin's lifespan task auto-times-out stale rows per `on_timeout`.
- No notification fan-out. Configure a webhook (Slack / Discord / PagerDuty) and the inbox events fire there too.
- No audit log. Every approval decision is a row in `decisions` + `approvals` with operator id and reason.

Apache 2.0. Local-first. `github.com/amitbidlan/zistica-lumin`.
