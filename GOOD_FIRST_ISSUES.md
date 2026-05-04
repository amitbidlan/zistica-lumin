# GOOD FIRST ISSUES — Ready to Create on GitHub

Create these issues after v1 ships (Docker image working).
Copy-paste each one into GitHub Issues exactly as written.
Add labels as specified.

---

## ISSUE 1

**Title:** Add Haystack framework integration

**Labels:** `good first issue`, `enhancement`, `framework-integration`

**Body:**
```
## What
Add a Lumin integration for the Haystack agent framework.

## Why
Haystack is widely used for RAG pipelines. Users have requested it.

## What to build
File: `packages/sdk-python/lumin/integrations/haystack.py`

Should capture:
- Every Pipeline.run() call as a root trace
- Each component execution as a child span (name = component name)
- Input and output of each component
- Errors with full message

## How to test
```python
from lumin.integrations.haystack import instrument_haystack
instrument_haystack()

from haystack import Pipeline
# ... your pipeline code
pipeline.run({"query": "test"})
# Trace should appear in Lumin dashboard
```

## Reference
Look at `packages/sdk-python/lumin/integrations/langchain.py` for how an existing integration works.

## Acceptance criteria
- [ ] Integration file created at the path above
- [ ] Tests added in `packages/sdk-python/tests/integrations/test_haystack.py`
- [ ] All existing tests still pass
- [ ] CHANGELOG.md updated
```

---

## ISSUE 2

**Title:** Add cost calculation for Anthropic Claude models

**Labels:** `good first issue`, `enhancement`

**Body:**
```
## What
When a span uses an Anthropic Claude model, calculate and store the cost_usd.

## Why
Currently cost_usd is None for Anthropic models. Users want to track Claude costs.

## What to build
File: `packages/sdk-python/lumin/cost.py`

Add Claude model pricing:
- claude-3-5-sonnet-20241022: input $3/M tokens, output $15/M tokens
- claude-3-5-haiku-20241022: input $0.80/M tokens, output $4/M tokens
- claude-3-opus-20240229: input $15/M tokens, output $75/M tokens

Reference: https://www.anthropic.com/pricing

## Acceptance criteria
- [ ] Cost calculated correctly for Claude models
- [ ] Tests added for each model
- [ ] Pricing source documented in code comment with date
- [ ] All existing tests still pass
```

---

## ISSUE 3

**Title:** Improve error message when Lumin server is unreachable

**Labels:** `good first issue`, `dx`

**Body:**
```
## What
When the Lumin server is not running and a developer tries to use the SDK,
show a helpful message instead of silently dropping spans.

## Current behavior
Spans are dropped silently. Developer has no idea Lumin is not running.

## Desired behavior
Log a single warning (only once, not on every span) like:
```
[Lumin] Cannot connect to http://localhost:8000. 
Spans will be dropped until the server is available.
Run: docker run -p 3000:3000 zistica/lumin
```

## Rules
- Warning logged only ONCE per session (not on every failed span)
- Agent still continues working — no exception raised
- Warning goes to Python logging module at WARNING level, not print()

## File to edit
`packages/sdk-python/lumin/exporter.py`

## Acceptance criteria
- [ ] Warning logged exactly once when server is unreachable
- [ ] Warning not repeated on subsequent failures
- [ ] Agent function still returns normally
- [ ] Test added verifying warning appears once
```

---

## ISSUE 4

**Title:** Add span type icons to dashboard timeline

**Labels:** `good first issue`, `dashboard`

**Body:**
```
## What
In the span timeline on the trace detail page, add an icon next to each span
based on its type.

## Why
Currently all spans look the same. Icons help users quickly identify what
each step is doing.

## Icons to use (from lucide-react, already installed)
- `llm` type → Brain icon
- `tool` type → Wrench icon
- `retrieval` type → Search icon
- `memory` type → Database icon
- `custom` type → Box icon

## File to edit
`packages/dashboard/components/SpanRow.tsx`

## Acceptance criteria
- [ ] Correct icon shown for each span type
- [ ] Icons are the same size and aligned consistently
- [ ] Looks good in both light and dark mode
- [ ] No new npm dependencies added
```

---

## ISSUE 5

**Title:** Write integration test for CrewAI

**Labels:** `good first issue`, `testing`

**Body:**
```
## What
Add a proper integration test for the CrewAI integration.

## Why
The CrewAI integration exists but has no automated test.
A bug could be introduced and we would not know.

## What to build
File: `packages/sdk-python/tests/integrations/test_crewai.py`

Test should:
1. Create a simple CrewAI crew with one agent and one task
2. Run the crew
3. Verify the trace was created in Lumin
4. Verify the spans have correct parent-child relationship
5. Verify LLM span has model and token info

## Notes
- Use `pytest-mock` to mock the actual LLM calls (no real API key needed)
- Look at existing tests in `packages/sdk-python/tests/` for patterns

## Acceptance criteria
- [ ] Test file created
- [ ] Tests pass without a real API key (mock the LLM)
- [ ] Tests fail if CrewAI integration is broken (actually tests something)
- [ ] CI pipeline runs this test
```

---

## ISSUE 6

**Title:** Add Japanese README (README.ja.md)

**Labels:** `good first issue`, `documentation`, `japanese`

**Body:**
```
## What
Create a Japanese version of the README: `README.ja.md`

## Why
Lumin targets Japanese enterprise users. A Japanese README makes
the project more accessible to Japanese developers.

## What to write
Direct translation of README.md into natural Japanese.
Not machine translation — please write in natural Japanese developer style.

Technical terms:
- trace → トレース
- span → スパン
- agent → エージェント
- dashboard → ダッシュボード
- observability → 可観測性

## Acceptance criteria
- [ ] `README.ja.md` created in root of repo
- [ ] All sections from README.md translated
- [ ] Natural Japanese (not machine translation)
- [ ] Code blocks unchanged (keep English)
- [ ] Link to README.ja.md added in README.md
```

---

## ISSUE 7

**Title:** Add LlamaIndex integration

**Labels:** `help wanted`, `enhancement`, `framework-integration`

**Body:**
```
## What
Add a Lumin integration for LlamaIndex.

## Why
LlamaIndex is widely used for RAG. Many Lumin users also use LlamaIndex.

## What to build
File: `packages/sdk-python/lumin/integrations/llama_index.py`

LlamaIndex has a callback system. Use it to capture:
- Query engine calls as root traces
- Retrieval steps as retrieval spans
- LLM calls as llm spans (with tokens and cost)
- Embedding calls as embedding spans

## Reference
- LlamaIndex callback docs: https://docs.llamaindex.ai/en/stable/module_guides/observability/
- Look at existing integrations in `packages/sdk-python/lumin/integrations/`

## This is harder than a `good first issue`
If you have not contributed to Lumin before, start with a `good first issue` first.

## Acceptance criteria
- [ ] Integration works with LlamaIndex SimpleDirectoryReader + VectorStoreIndex
- [ ] Integration works with LlamaIndex ReActAgent
- [ ] Tests added
- [ ] CHANGELOG.md updated
```

---

## HOW TO CREATE THESE ON GITHUB

1. Go to `github.com/amitbidlan/zistica-lumin/issues/new`
2. Copy the Title and Body from above
3. Add the Labels listed (create labels first if they don't exist)
4. Submit

## LABELS TO CREATE FIRST

Go to github.com/amitbidlan/zistica-lumin/labels and create these:

| Label | Color | Description |
|---|---|---|
| `good first issue` | #7057ff | Good for newcomers |
| `help wanted` | #008672 | Extra attention needed |
| `bug` | #d73a4a | Something is broken |
| `enhancement` | #a2eeef | New feature or improvement |
| `documentation` | #0075ca | Documentation improvements |
| `framework-integration` | #e4e669 | New framework integration |
| `dashboard` | #fbca04 | Dashboard / frontend changes |
| `testing` | #bfd4f2 | Tests and QA |
| `dx` | #d4c5f9 | Developer experience |
| `japanese` | #f9d0c4 | Japanese language / localization |
| `needs-triage` | #ededed | Needs maintainer review |
| `wont-fix` | #ffffff | Will not be fixed |
| `duplicate` | #cfd3d7 | Already reported |
