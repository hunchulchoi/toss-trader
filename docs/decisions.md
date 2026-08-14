# Architecture Decisions

## ADR-001 Agent worktree isolation

- Status: accepted
- Date: 2026-08-14

### Decision

Use Herdr for terminal/session state and assign one Git worktree per role:

- Codex builder: `agent/codex`
- Cursor reviewer/UI: `agent/cursor`
- agy researcher: `agent/agy`

The canonical checkout remains the integration point for `main`.

### Reason

- Concurrent edits do not overwrite each other.
- Review remains independent from implementation.
- Role names survive future agent or model changes.

### Coordination consequence

Git branches do not provide real-time shared files. `docs/tasks.md` on `main` is
canonical; agents fetch/rebase before claiming work and hand off with a commit
hash. Herdr prompts carry urgent state between sync points.
