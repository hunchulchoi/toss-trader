# Agent Tasks

The canonical task board is this file on `main`. Branch copies are snapshots;
agents must sync from `main` before claiming or handing off work.

## TODO

### DATA-002 Resume OpenDART backfill

- Owner: codex/cursor
- Status: TODO
- Goal: resume idempotent CFS/OFS collection for the remaining symbols and then
  collect event pages after the provider stops resetting connections
- Acceptance: 200-symbol completeness manifest, zero failed requests, event
  coverage manifest, and a final read-only audit

## IN PROGRESS

No active tasks.

## REVIEW

No tasks awaiting review.

## DONE

### DATA-003 Prospective PIT event collection

- Owner: codex
- Status: DONE
- Result: added a restart-safe daily collector that refreshes the recent
  DataGo session ledger, resolves future Korean sessions through the Toss
  calendar, and checkpoints OpenDART events one date at a time; setup-v2 now
  reads covered event state and the reserved official-flow table
- Checks: 253 unit tests; scoped Ruff; live Infisical-injected OpenDART backfill
  populated 12,711 events with zero missing `available_at` values
- Risks: per-symbol foreign/institutional flow remains fail-closed because no
  authorized official API source is configured; KRX Data Marketplace rejected
  unauthenticated automation with `LOGOUT`

### STRAT-005 Activate strict setup-v2 entry gate

- Owner: codex
- Status: DONE
- Result: every paper BUY candidate now requires 200 daily candles and strict
  setup-v2 approval before RiskManager or Hermes; missing PIT flow/events are
  recorded as `setup-v2-block`, while SELL and risk reduction remain available
- Checks: dedicated missing-input, complete-input, runtime preflight, and SELL
  bypass tests plus full regression suite
- Risks: setup-v2 currently gates MA-generated BUY candidates rather than
  generating independent entries; executable v2 integer sizing and persisted
  open/cluster heat are not connected, so strict missing inputs intentionally
  keep new BUY at zero

### STRAT-004 Setup-v2 weekly replay

- Owner: codex
- Status: DONE
- Result: replayed 2026-08-10 through 2026-08-14 with 200-bar Toss
  warmup, price setups, hard filters, and integer sizing; strict v2 stayed
  fail-closed because valid PIT flow and event data are unavailable
- Checks: 196 eligible symbols per session, 980 symbol-sessions, 34 price setups,
  32 hard-filter survivors, 0 strict approvals; 200 warmup requests had 0 failures
- Risks: current 200-symbol universe is not PIT membership; the 9-trade one-day
  counterfactual is not a v2 exit rule or shared-cash portfolio backtest

### OPS-003 Toss holiday schedule gate

- Owner: codex
- Status: DONE
- Result: all three n8n schedule paths query the Toss KR market calendar before
  work and stop before scan, paper cycles, Hermes, and Telegram on holidays;
  manual and authenticated operator triggers remain available
- Checks: 245 unit tests; three exported workflow graphs and JSON syntax checked
- Risks: a Toss calendar outage fails closed and marks the scheduled n8n
  execution failed after three attempts; updated workflow exports still require
  import/reload before the running n8n instance uses them

### OPS-002 Hermes instrument names

- Owner: codex
- Status: DONE
- Result: Hermes trade signals now include company `name`; daily cycle payloads
  include `instruments[{symbol,name}]`, and result items include their name while
  preserving existing symbol fields
- Checks: 243 unit tests; scoped Ruff passed
- Risks: a symbol missing from both `market_symbols` and the Toss stock-info API
  now fails before Hermes analysis instead of sending an ambiguous code

### DATA-001 Official PIT rebuild

- Owner: codex/cursor
- Status: DONE
- Result: implemented versioned official-source raw facts, conservative observed-session availability, CFS-preferred TTM EPS/BPS snapshots, bounded post-disclosure event blocks, and completed a 483-session full-market raw ledger; OpenDART backfill is resumable but partial after provider resets
- Checks: 242 unit tests, scoped Ruff, full DataGo collection, partial OpenDART collection, SQLite invariant queries, Cursor blocker review
- Risks: OpenDART is partial at 31/200 symbols after connection resets; official instrument-master access remains HTTP 403, so security type is UNKNOWN and TopN stays fail-closed; forward consensus remains disabled

### DOC-001 External debate verification

- Owner: codex/cursor
- Status: DONE
- Result: independently checked 2026-08-14 index and stock closes, retired the false Doosan no-rebound claim, distinguished KRX close from Toss extended-session last price, and separated the completed MA exit benchmark from the pending actual-trade P3
- Checks: Naver daily API, read-only Toss minute/daily SQLite comparison, Cursor cross-review
- Risks: flow, FX, internal paper equity/cycle counts, and market-cause narratives remain unverified

### STRAT-003 Expanded dataset and exit counterfactual

- Owner: codex/cursor/agy
- Status: DONE
- Result: validated 200-symbol read-only SQLite data, reran the 5/10 open-cap matrix, diagnosed setup-v2 price and integer sizing, and compared dead-cross, 5/10/15-bar holds, and ATR2 exits across 400,000 minute bars
- Tests: 6 exit-counterfactual boundaries and full suite 237 passed; 5bps and 10bps full-dataset matrices completed
- Risks: one-week minute sample, instrument types not normalized, no PIT flow/event/gap data, independent one-share simulations rather than shared-cash portfolio, and no continuation/Hermes replay; no production policy changed

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
