# Agent Tasks

The canonical task board is this file on `main`. Branch copies are snapshots;
agents must sync from `main` before claiming or handing off work.

## TODO

No unassigned tasks.

## IN PROGRESS

No active tasks.

## REVIEW

No tasks awaiting review.

## DONE

### STRAT-001 Risk-cap hypothesis backtest

- Owner: codex/cursor/agy
- Status: DONE
- Result: read-only stored-candle sensitivity compared open 5/10/unlimited across 50 symbols; results reversed by MA window, daily 5 was not the main bottleneck, so production risk values remain unchanged
- Tests: MA20/60 reported 0 trades from 61-63 bar warmup; pre-fixed MA5/20 and MA10/30 cap matrices completed; Cursor and agy reviewed interpretation
- Risks: three-month single-regime sample, code-order signal selection, few completed trades, no setup-v2 PIT flow, and no continuation replay; require at least 200 and preferably 500 bars for preregistered OOS comparison

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
