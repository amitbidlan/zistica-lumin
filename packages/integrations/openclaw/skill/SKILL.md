---
name: synaptic
description: >
  Observe and debug your OpenClaw agent with Synaptic — local-first
  AI observability. Use when the user asks:
  - "show my agent traces"
  - "why did my agent fail"
  - "how much did my last run cost"
  - "open synaptic"
  - "debug my agent"
  - "show recent traces"
  - "what did my agent do"
  - "trace details for <id>"
---

## Synaptic — Local Agent Observability

Synaptic records every LLM call, tool call, and decision your OpenClaw
agent makes. View them at <http://localhost:3000>. Data never leaves
the machine — everything runs in a single Docker container.

This skill provides commands to check Synaptic's status and pull
trace data without leaving the chat.

### Check if Synaptic is running

Run: `node $SKILL_DIR/synaptic.mjs status`

Returns one of:
- `Synaptic is running on http://localhost:8000`
- `Synaptic is not running. Start with: docker run -p 3000:3000 -p 8000:8000 zistica/synaptic`

### Show recent traces

Run: `node $SKILL_DIR/synaptic.mjs traces`

Returns the last 5 agent traces formatted as:
```
Recent agent traces:
  1. openclaw_session  3.2s  $0.0023  OK
  2. openclaw_session  1.8s  $0.0011  OK
  3. openclaw_session  5.1s  $0.0089  ERROR
  …
Open http://localhost:3000/traces for the full list.
```

### Show one trace's spans

Run: `node $SKILL_DIR/synaptic.mjs trace <id>`

Returns the trace metadata plus every span with type, model, tokens,
cost, and any error message.

### Configuration

Set `SYNAPTIC_HOST` to point at a non-default Synaptic instance:

```bash
export SYNAPTIC_HOST=http://my-synaptic:8000
```

Default is `http://localhost:8000`.

### Start Synaptic (if not running)

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/synaptic
```

Then open <http://localhost:3000>.
