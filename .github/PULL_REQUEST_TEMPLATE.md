<!--
Thanks for contributing to Synaptic!
Fill in the sections below. Delete sections that don't apply.
-->

## Summary

<!-- One or two sentences describing what this PR does and why. -->

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would change existing behavior)
- [ ] Documentation update
- [ ] Refactor (no functional change)
- [ ] Test-only change
- [ ] Build/CI/tooling

## Linked issue

<!-- Closes #123, Fixes #456, etc. -->

## How has this been tested?

<!-- Describe the tests you ran and how to reproduce them. -->

- [ ] Existing tests pass locally (`pytest` for Python, `npm test` for TS)
- [ ] New tests added for new behavior
- [ ] Manual verification (describe below)

## Session completion checklist

<!-- From docs/Development_Rules.md — required for any code change. -->

- [ ] All tests in the affected package pass
- [ ] No tests skipped or marked `xfail` without a documented reason
- [ ] No `TODO` comments without a linked GitHub issue
- [ ] No hardcoded values (use config / env vars)
- [ ] No `print()` statements in production code (use `logging`)
- [ ] No commented-out code in modified files
- [ ] Package README updated if the public API changed
- [ ] `requirements.txt` / `package.json` updated if deps changed

## Synaptic-specific checks

- [ ] If touching SDK code: agent never raises if Synaptic server is unreachable (Rule 7)
- [ ] If touching ingest path: spans isolated by `project_id`
- [ ] If touching APPI / enterprise code: covered by tests
- [ ] No data leaves the local laptop in default OSS mode

## Screenshots / logs

<!-- For UI changes or debugging output, paste relevant snippets. -->

## Notes for reviewer

<!-- Anything reviewers should focus on, or known limitations. -->
