# Toss Trader Agent Rules

## Repository

- This repository is the canonical source.
- Read `docs/tasks.md` before starting work.
- Make the smallest relevant change and preserve unrelated user changes.
- Follow the existing architecture, naming, and test conventions.
- Never commit secrets, `.env` files, credentials, tokens, or generated noise.
- Never run destructive database or Git commands.
- Do not add production dependencies unless the task requires them.

## Git

- Each agent works only in its assigned branch and worktree.
- Do not force-push, amend, or rebase another agent's branch.
- Commit one completed checkpoint at a time.
- Before committing, inspect `git status` and stage only task files.
- Integration into `main` is performed from the canonical checkout after review.

## Coordination

- Task ownership and handoff state live in `docs/tasks.md` on `main`.
- Architectural decisions live in `docs/decisions.md`.
- A branch copy of these files is a snapshot. Fetch or rebase from `main` before
  claiming a task; do not assume another worktree's unmerged edits are visible.
- On completion report: result, changed files, tests, risks, and commit hash.

## Production Safety

- Inspect live state before mutation.
- Do not deploy, restart containers, publish n8n workflows, trade, or mutate a
  database unless the user explicitly authorized that operation.
- Keep `TRADING_ENABLED=false` during tests unless the user explicitly enables
  live trading.
- Use Infisical command injection for runtime secrets; never print secret values.

## Roles

### Codex — Builder

- Primary implementation, backend, database code, refactoring, tests, bug fixes.
- Branch: `agent/codex`.

### Cursor — Reviewer and UI

- Review Codex diffs, frontend/UI, small corrective edits, integration checks.
- Do not modify the Codex branch.
- Branch: `agent/cursor`.

### agy — Researcher

- Investigate alternatives, APIs, architecture, and isolated prototypes.
- Avoid production implementation unless explicitly assigned.
- Branch: `agent/agy`.
