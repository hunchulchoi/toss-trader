# Architecture Decisions

## ADR-006 Daily analysis runs in the main Hermes container

- Status: accepted
- Date: 2026-08-19

### Decision

The n8n closing workflow persists a daily panel job after both paper portfolios
finish. A no-agent Hermes cron script claims the job and invokes three fixed,
read-only Cursor models in two isolated rounds:

- Grok `cursor-grok-4.6-high-fast`: quant analyst
- Grok `cursor-grok-4.6-high-fast`: skeptic / anomaly detector
- Gemini `gemini-3.7-flash-high`: Risk Manager

Hermes `openai-codex/gpt-5.6-terra` judges the six labeled responses. Every
opinion and provider-reported token count is persisted before the final judge
text is sent through the existing Alertmanager-to-Telegram path.

### Reason

Hermes `terminal` runs in a separate Python container without Cursor binaries.
The authenticated `cursor-agent` exists only in the main Hermes container.
Running the fixed script there preserves that boundary and avoids exposing the
Docker socket, Cursor credentials, or arbitrary shell execution to n8n.
The runner pins `HOME=/opt/data` and `XDG_CONFIG_HOME=/opt/data/.config` for
Cursor subprocesses because the Hermes scheduler inherits the root wrapper's
environment even though jobs execute as the `hermes` user.

### Failure contract

- Any missing model round fails the panel and suppresses the final report.
- Opinions completed before a peer failure remain stored with token usage.
- A running job may be reclaimed after 30 minutes; opinion writes are
  idempotent by `(panel_id, stage)`.
- The closing cycle queue and panel execution are separate. n8n never waits for
  model latency.

## ADR-005 Official PIT inputs remain fail-closed

Status: accepted 2026-08-15

- Preserve OpenDART CFS and OFS account facts; derive valuation in a separate,
  versioned snapshot and prefer CFS.
- Unknown filing time becomes next observed market session 08:00 KST.
- Unscheduled events may only block after availability, never before receipt.
- DataGo archive rows are not assumed available at the historical close.
- `security_type=UNKNOWN` forbids TopN eligibility. Name heuristics are not an
  official instrument master.
- Forward-consensus multipliers remain x1.0 until licensed PIT snapshots exist.

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
