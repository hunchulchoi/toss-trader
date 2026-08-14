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

## DONE

### STRAT-002 Setup hypothesis v2

- Owner: codex/cursor/agy
- Status: DONE
- Result: pure evaluator requires one price setup plus PIT 5-day flow reversal; valuation boost is disabled; sizing uses 0.5% trade, 2% open, 1% cluster heat with ATR, Toss costs, slippage, integer lots, cash and order caps
- Tests: 20 dedicated boundary tests; full suite 231 passed; Cursor and agy final review passed with 0 blockers
- Risks: sector symbols, persisted PIT flow, point-in-time valuation, event calendar, gap threshold, stop derivation, cluster mapping, and automatic regime are not yet provided; not connected to orders

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
