# Agent Tasks

The canonical task board is this file on `main`. Branch copies are snapshots;
agents must sync from `main` before claiming or handing off work.

## TODO

No unassigned tasks.

## IN PROGRESS

No active tasks.

## REVIEW

### STRAT-001 Risk-cap hypothesis backtest

- Owner: codex
- Status: REVIEW
- Result: portfolio backtest now models order, position, UTC daily-buy, and open-position limits; deterministic scenarios reject daily-cap expansion and show position-cap duplication
- Tests: 209 unit tests passed; changed-file Ruff passed
- Risks: stored-candle run blocked until compromised Infisical token revoke/rotate is confirmed

### STRAT-002 Setup hypothesis v2

- Owner: codex
- Status: REVIEW
- Result: pure 200-day setup evaluator covers pullback, confirmed oversold reversal, flow overlap, prohibitions, relative valuation tiers, and capped sizing reference
- Tests: dedicated boundary tests; full suite pending
- Risks: sector symbols, flow, point-in-time valuation, event calendar, gap threshold, stop derivation, and volatility regime are not yet provided; not connected to orders

## DONE

### OPS-001 Intraday Telegram JSON fix

- Owner: codex/cursor
- Result: explicit n8n JSON fields; repetitive `max-open-positions` notices suppressed
- Tests: 204 unit tests; 2026-08-14 15:15 KST live execution succeeded
- Commits: `18f06bd`, `3ab58db`

## Task Template

```text
### TASK-ID Short title
Owner: codex | cursor | agy
Branch: agent/<role>
Status: TODO | IN PROGRESS | REVIEW | DONE

Goal:

Allowed paths:

Do not touch:

Acceptance checks:

Handoff:
- Result:
- Tests:
- Risks:
- Commit:
```
