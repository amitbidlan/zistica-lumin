# Why your agent is one IPI away from leaking customer data — and how to stop it

Two months ago I asked our customer-support bot a question. Then I asked it to look up another customer's order. *Then* I asked it the first question again.

The reply quoted the second customer's data.

The model didn't lie. The retrieval system didn't have a bug. The agent's tool calls were all correct. The leak was *legitimate output*. Yesterday's tool result poisoned today's context, and there was no boundary in our system that said "wait — those facts belong to a different conversation."

This is **indirect prompt injection** (IPI), or more precisely the *cross-session memory bleed* variant that nobody talks about. OWASP LLM01 covers prompt injection from the user; almost no one covers prompt injection *from the agent's own retrieval surface* leaking across user boundaries. This is the more dangerous shape because the model is reasoning correctly — only the data has crossed a tenant line you forgot to draw.

## How it happens (three failure modes)

**Mode 1: shared retrieval store.** RAG-flavored agents often have a single vector store. You filter by `user_id` at query time. Then someone forgets to. Or someone doesn't filter on a *summarization* step that runs after retrieval. Or the embedding similarity scoops a chunk from another tenant whose `user_id` *was* set correctly but whose content matched.

**Mode 2: tool-result poisoning.** A tool call to your CRM returns a list of "recent customers" without filtering. The model summarizes the list back. The current user wasn't supposed to see the others.

**Mode 3: long-running session memory.** The bot has a "remember this for later" tool. User A says "remember my SSN is 555-12-3456 for next time." User B inherits the same memory store because of a session-scoping bug.

All three look identical from the outside: the model produced text that quotes data the current user shouldn't see.

## What detection looks like

Lumin's IPI sniffer (Slice 3 §6.9) runs at `after_tool_call` — *after* a tool returns, *before* its content reaches the model on the next turn. It does three things in sequence:

1. **HTML / markdown strip.** Modern IPI hides the malicious payload in HTML comments, image alt text, or tag content the model rehearses without parsing. Strip first, score second.

2. **Prompt Guard 2 + ASCII smuggling check.** Run the cleaned text through Meta's classifier. If the result is `score > 0.7`, the tool result *is itself a prompt-injection attack* — even though the original tool call was clean. We rewrite the tool's content with a redaction marker and return that to the model.

3. **Cross-passage scoring.** When a tool returns a long blob (search results, documentation, ticket bodies), score each passage separately. A 50KB doc with one malicious paragraph hidden mid-way is more common than a fully-malicious return. The sniffer surfaces the offending span.

That stops modes 1 and 2 at the data boundary. Mode 3 — cross-session memory bleed — needs an additional layer: the **session vault** detector (in design, ships in the next slice). It records whose data is whose at write-time, then on every reply check whether the response contains any vault entry from a different `user_id`. Heuristic, not airtight, but it catches the textbook attack.

## Why provider moderation doesn't catch this

OpenAI Moderation, Anthropic safety, and similar provider-side filters look at one message at a time. They check whether *this* message is harmful. They don't know the message contains another user's customer ID, because they don't know who *this* user is.

You need a firewall that:
1. Sees the tool result before the model does
2. Knows which user is talking
3. Has a record of what data belongs to whom

Lumin does (1) at `after_tool_call`, (2) via `session_id` propagated through every decide call, and (3) is what the session vault is being built for.

## What to do today

1. Install Lumin. `openclaw plugins install @lumin-io/openclaw-diagnostics`. The OWASP LLM Top 10 starter pack ships an IPI rule in shadow mode. Watch what it catches in your traffic for a week.
2. Promote the IPI rule to enforce once you trust the false-positive rate.
3. Watch this space — the session vault detector ships in the next slice and is built directly on top of the firewall's existing identity-binding.

The dirty truth of agent security is that the model is rarely the weak point. The weak point is the seam between the model and your data. Lumin sits at that seam.

`github.com/amitbidlan/zistica-lumin`
