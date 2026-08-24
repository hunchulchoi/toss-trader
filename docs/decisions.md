# Architecture Decisions

## ADR-008 Hermes uses strategy gates as paper evidence

- Status: accepted
- Date: 2026-08-24

### Decision

Keep Rule on strict setup-v2.3. Give Hermes a separate, bounded paper-only pool:
Rule selections first, then D-1 liquidity-ranked static-eligible common stocks,
up to 30 symbols. Treat `missing-price-setup`, `rsi-chase`, and
`falling-knife` as advisor evidence rather than automatic Hermes vetoes.

Mandatory PIT/event data, imminent events, gap checks, integer-share sizing,
heat, cash, order/account Risk, market session, and the 09:30 entry cutoff stay
deterministic and fail-closed. Every Hermes entry is labeled
`hermes-experimental` and remains isolated from Rule results and real trading.
Rule retains 0.5% per-trade, 2% open, and 1% UNKNOWN-cluster heat. Hermes paper
uses 2% per-trade and 6% open/UNKNOWN-cluster heat; both retain ATR stops,
integer shares, the KRW 700,000 order ceiling, cash, and account RiskManager.

### Reason

The prior Hermes path could only review a signal after Rule's full strategy gate
approved it. With a KRW 1,000,000 portfolio and rare D-1 setups, both ledgers
therefore repeated the same zero-signal result and produced no independent
strategy sample. The new split tests discretionary evidence use without
weakening data integrity or execution safety.

### Failure contract

- Missing PIT/event facts or malformed market data never become model judgment.
- Hermes cannot override sizing, cash, RiskManager, calendar, or entry time.
- Advisor failure remains a rejected paper decision with no fill.
- Rule and Hermes results are asymmetric experiments, not direct A/B evidence.

## ADR-007 Opening entries use a D-1 setup-first known pool

- Status: accepted
- Date: 2026-08-21

### Decision

Build the entry pool by observing the prior completed session's KRX rows at
08:35 KST and intersecting them with `market_symbols`. Evaluate every statically eligible symbol's
completed 200-day history before applying the candidate cap, then rank actual
price-setup passes by D-1 trading value. Freeze that result for the next Seoul
session, including a valid zero-symbol result.

Same-day Toss and afternoon KRX rankings no longer change the production entry
membership. They may be retained later as non-authoritative shadow research.
The 10-minute entry window, opening gap check, sizing, heat, cash, account Risk,
and held-position SELL paths remain unchanged.

### Reason

The former process selected a realtime Top30 first and then looked for a rare
daily setup inside it. A five-session replay over the known pool observed only
34 price setups in 980 evaluations, so the ordering created avoidable candidate
misses and made afternoon membership changes unusable after the entry window.

### Failure contract

- D-1 rows must have `available_at <= decision time`.
- Acquisition or parse errors fail the run and do not create a success cache.
- Completed evaluation with no setup is a normal zero-symbol success.
- No late entry, opening-price backfill, or retroactive fill is allowed.

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
## ADR-009 Hermes reviews Hunter without execution authority

Status: accepted 2026-08-24

- Hunter remains `strategyInput=false` and `shadowOnly=true`.
- Hermes reviews the daily selected set once as `approve/watch/reject`; it cannot
  call RiskManager, create a signal, fill an order, or change Hunter ranking.
- Persist the full opinion and token usage separately from the deterministic plan.
- Compare all Hunter plans with the Hermes-approved subset using stored 1m candles.
  Same-bar stop/target ambiguity resolves to stop and no fees or slippage are claimed.
- Hermes unavailability is an audit event, never a paper-cycle failure.
