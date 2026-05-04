---
name: lumin
description: >-
  Observe and debug your OpenClaw agent with Lumin — local-first AI
  observability. Records every LLM call, tool call, and decision; view
  at localhost:3000. Use when the user asks: "show my agent traces",
  "why did my agent fail", "how much did my last run cost", "open
  lumin", "debug my agent", or "show recent traces".
version: 0.1.0
metadata:
  openclaw:
    primaryEnv: LUMIN_HOST
    envVars:
      - name: LUMIN_HOST
        required: false
        description: >-
          Lumin API base URL. Defaults to http://localhost:8000.
          Override only if Lumin is running on a non-default host.
      - name: LUMIN_API_KEY
        required: false
        description: >-
          Optional bearer token. Required only when talking to a hosted
          Lumin instance; not used for the default localhost setup.
    requires:
      anyBins:
        - node
        - bun
    emoji: "📊"
    homepage: https://github.com/amitbidlan/zistica-lumin
---

## Lumin — Local Agent Observability

Lumin records every LLM call, tool call, and decision your OpenClaw
agent makes. View them at <http://localhost:3000>. Data never leaves
the machine — everything runs in a single Docker container.

This skill provides three commands that hit Lumin's local HTTP API
so you can check status and pull trace data without leaving the chat.

### Check if Lumin is running

Run: `node $SKILL_DIR/lumin.mjs status`

Returns one of:

- `Lumin is running on http://localhost:8000`
- `Lumin is not running on …. Start with: docker run -p 3000:3000 -p 8000:8000 zistica/lumin`

### Show recent traces

Run: `node $SKILL_DIR/lumin.mjs traces`

Returns the last 5 agent traces formatted like:

```
Recent agent traces:
  1. openclaw_session  3.2s  $0.0023  OK
  2. openclaw_session  1.8s  $0.0011  OK
  3. openclaw_session  5.1s  $0.0089  ERROR
  …
Open http://localhost:3000/traces for the full list.
```

### Show one trace's spans

Run: `node $SKILL_DIR/lumin.mjs trace <id>`

Returns the trace metadata plus every span with type, model, tokens,
cost, and any error message.

### Wire OpenClaw to Lumin

This skill only reads from Lumin. To get your OpenClaw agent's
traces *into* Lumin, install the companion exporter:

```bash
npm install @lumin-io/openclaw
```

…and pass `luminProcessor()` into your OpenClaw OTel config. See
<https://github.com/amitbidlan/zistica-lumin/tree/main/packages/integrations/openclaw>.

### Start Lumin (if not running)

```bash
docker run -p 3000:3000 -p 8000:8000 zistica/lumin
```

Then open <http://localhost:3000>.
