# Contributing to Lumin

First off — thank you. Seriously. Every bug report, every PR, every Discord message makes Lumin better for everyone building AI agents.

This document tells you everything you need to know to contribute.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Your First Contribution](#your-first-contribution)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Pull Request Process](#pull-request-process)
- [Issue Guidelines](#issue-guidelines)
- [Commit Messages](#commit-messages)
- [Getting Help](#getting-help)

---

## Code of Conduct

This project follows a simple rule: **be kind**.

We welcome contributors regardless of experience level, background, or nationality. Japanese and English both welcome. If someone is rude or hostile, they will be removed from the community.

Full details in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## Ways to Contribute

You do not have to write code to contribute. All of these matter:

**If you are new to open source:**
- Report a bug you found
- Improve documentation or fix a typo
- Answer a question in Discord
- Share Lumin with someone who builds AI agents

**If you are comfortable with code:**
- Pick up a `good first issue` from GitHub Issues
- Add a new framework integration (Haystack, Vertex AI, etc.)
- Write a missing test
- Improve error messages

**If you are experienced:**
- Review pull requests
- Design new features (open an issue first)
- Help with Japanese translations
- Performance improvements

---

## Your First Contribution

**Step 1: Find something to work on**

Look for issues labeled:
- `good first issue` — small, well-defined, great starting point
- `help wanted` — we want community input on these
- `documentation` — writing and docs improvements

Do not start large changes without discussing first. Open an issue, explain what you want to do, wait for a maintainer to say go ahead. This saves everyone's time.

**Step 2: Comment on the issue**

Before starting, comment on the issue: "I'd like to work on this." This prevents two people doing the same work.

**Step 3: Fork and branch**

```bash
# Fork on GitHub, then:
git clone https://github.com/YOUR_USERNAME/lumin.git
cd lumin
git checkout -b your-branch-name

# Branch naming:
# feat/add-haystack-integration
# fix/span-context-propagation
# docs/improve-quickstart
# test/add-crewai-integration-tests
```

**Step 4: Read the docs**

Before touching any code, read:
- `docs/Lumin_Technical.md` — architecture decisions are already made, follow them
- `docs/Development_Rules.md` — non-negotiable rules for this codebase

**Step 5: Make your change, test it, open a PR**

See sections below for details.

---

## Development Setup

### Requirements

- Python 3.11+
- Node.js 20+
- Docker (for running the full stack)
- Git

### Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/lumin.git
cd lumin

# One-shot install — Python SDK + API + all 5 Node packages.
# Creates packages/api/.venv, runs npm ci across the workspace.
./setup.sh

# Run tests to verify setup
cd packages/api && .venv/bin/pytest && cd ../..
cd packages/sdk-python && .venv/bin/pytest && cd ../..
```

If you want the manual sequence (or `./setup.sh` doesn't fit your
workflow), the order matters because the API imports `lumin` from
the sibling `packages/sdk-python`, **not** from PyPI:

```bash
# 1. Local SDK first (editable). PyPI has an unrelated package
#    named "lumin" — never let pip resolve it from there.
pip install -e "packages/sdk-python[dev]"

# 2. API dependencies
cd packages/api && pip install -r requirements.txt && cd ../..

# 3. Node packages (lockfile-faithful install)
cd packages/dashboard && npm ci && cd ../..
cd packages/sdk-typescript && npm ci && cd ../..
cd packages/integrations/openclaw && npm ci && cd ../../..
cd packages/integrations/mastra && npm ci && cd ../../..
cd packages/integrations/voltagent && npm ci && cd ../../..
```

### Run Locally

```bash
# Option 1: Docker (recommended — runs everything)
docker build -t zistica/lumin .
docker run -p 3000:3000 zistica/lumin

# Option 2: Run services separately (for development)
# Terminal 1 — API
cd packages/api && uvicorn main:app --port 8000 --reload

# Terminal 2 — Dashboard
cd packages/dashboard && npm run dev

# Terminal 3 — Test your change
python -c "
import lumin
lumin.configure(host='http://localhost:8000')

@lumin.trace
def test_agent(input):
    return 'hello'

test_agent('world')
print('Open http://localhost:3000 to see the trace')
"
```

---

## Making Changes

### Rules (non-negotiable)

**1. Agent must never fail because of Lumin.**
If Lumin is down, the agent keeps running. Spans are dropped silently. No exception ever reaches agent code. This is the most important rule.

**2. Tests required.**
Every change needs tests. No exceptions. PRs without tests will not be merged.

**3. One thing per PR.**
Fix one bug OR add one feature OR improve one thing. Not all three. Small PRs get reviewed faster.

**4. Follow existing patterns.**
Look at how existing code is written. Match the style. Do not refactor things that are not broken.

**5. No new dependencies without discussion.**
Adding a dependency is a big decision. Open an issue first.

### What NOT to add in v1

These are out of scope right now. Do not build them:

- User authentication / login
- Redis, PostgreSQL, ClickHouse (use DuckDB and SQLite)
- APPI compliance / PII masking
- Billing or payments
- Kubernetes / Helm
- Multi-tenancy

If you want to discuss adding something big, open a `discussion` issue first.

---

## Pull Request Process

### Before opening a PR

```bash
# Run all tests
pytest packages/sdk-python/tests/ -v

# Run linting
ruff check packages/sdk-python/
ruff check packages/api/

# Type check
mypy packages/sdk-python/lumin/

# If you changed the dashboard
cd packages/dashboard && npm run build
```

All must pass. Do not open a PR with failing tests.

### PR checklist

Your PR description must include:

```
## What does this PR do?
[One sentence]

## Why?
[Why is this change needed?]

## How to test?
[Steps to verify this works]

## Checklist
- [ ] Tests added and passing
- [ ] No new external dependencies (or discussed in issue)
- [ ] Documentation updated if needed
- [ ] CHANGELOG.md updated
```

### Review process

- A maintainer will review within 3 business days (JST)
- We may ask for changes — this is normal, not rejection
- Once approved, a maintainer will merge
- Do not merge your own PR

### After your PR is merged

- Your name goes in CHANGELOG.md
- You are officially a Lumin contributor
- Say hi in Discord `#contributors`

---

## Issue Guidelines

### Reporting a bug

Use the **Bug Report** template when opening an issue. Include:

1. What you did
2. What you expected to happen
3. What actually happened
4. Your environment (OS, Python version, Lumin version)
5. Minimal code to reproduce the bug

A bug report without reproduction steps will be closed.

### Requesting a feature

Use the **Feature Request** template. Include:

1. What problem does this solve?
2. Who benefits from this?
3. How would it work?
4. Are you willing to implement it?

We will not add features that only one person needs. Community demand matters.

### Asking for help

Do not open GitHub issues for questions. Use:
- Discord `#help` channel
- GitHub Discussions

---

## Commit Messages

Format: `type(scope): description`

```
feat(sdk):     add @lumin.trace support for async generators
fix(api):      handle missing ended_at field in span ingestion
test(sdk):     add context propagation tests for concurrent tasks
docs(readme):  add Helicone comparison to competitive table
chore(docker): reduce image size from 1.2GB to 340MB
refactor(api): extract DuckDB connection into separate module
```

Types: `feat`, `fix`, `test`, `docs`, `chore`, `refactor`

Rules:
- Use lowercase
- No period at end
- Keep under 72 characters
- Reference issue number if applicable: `fix(sdk): handle timeout (#42)`

---

## Getting Help

| Channel | For |
|---|---|
| Discord `#help` | Questions about using Lumin |
| Discord `#contributing` | Questions about contributing |
| GitHub Issues | Bug reports, feature requests |
| GitHub Discussions | Architecture discussions, big ideas |

Discord invite: **[link coming soon]**

---

## Thank You

Every contribution matters — from fixing a typo to adding a new framework integration. Lumin is better because of people like you.

Built by [Zistica](https://zistica.com) · Apache 2.0 License
