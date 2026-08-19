# Agent Tasks

The canonical task board is this file on `main`. Branch copies are snapshots;
agents must sync from `main` before claiming or handing off work.

## TODO

### DATA-002 Resume OpenDART financial backfill

- Owner: codex/cursor
- Status: TODO
- Goal: resume idempotent CFS/OFS collection for the remaining symbols and then
  collect event pages after the provider stops resetting connections
- Acceptance: 200-symbol completeness manifest, zero failed requests, event
  coverage manifest, and a final read-only audit

## IN PROGRESS

No tasks in progress.

## REVIEW

### STRAT-009 Harden setup-v2 universe membership

- Owner: codex/cursor/agy
- Status: REVIEW
- Goal: separate acquisition errors from valid empty selections, keep mutable
  account/system Risk out of membership, and derive the authoritative universe
  from overfetched trading-amount ranks after static eligibility filtering
- Result: amount-only overfetch, static eligible reranking, setup filtering, valid
  empty caching, acquisition failure retry, mutable Risk execution-only, and
  local membership and n8n execution Risk separation implemented. The optional
  n8n universe contract remains policy v2-compatible for parity tests. Raw/
  eligible ranks are audit provenance, not exact replay inputs.
- Checks: 327 unit tests; scoped Ruff; JSON and n8n Code syntax; Git whitespace;
  isolated real Toss smoke; Cursor and agy reviews with zero MUST FIX findings
- Risks: production DB migration and service rollout remain pending. Optional
  n8n universe compatibility path is policy v2, while live trade stays backward-
  compatible with policy v1 and production membership makes no n8n call

## DONE

### STRAT-008 Align universe with setup-v2 price candidates

- Owner: codex
- Status: DONE
- Result: gated ranked symbols with completed-daily setup-v2 price rules before
  RiskManager, froze each successful selection for the Seoul trading date, kept
  held symbols for exits, and made an empty candidate set a successful no-BUY cycle
- Checks: 312 unit tests; scoped Ruff and Git whitespace checks; deployed
  automation healthy with restart 0 and `TRADING_ENABLED=false`; 13:55 Rule/Hermes
  cycles succeeded with zero failures and API errors
- Risks: first cycle may fetch daily history for every ranked symbol; today's
  pre-deployment successful universe remains cached, so the first live filtered
  selection will occur on the next Seoul trading date

### WEB-002 Cycle universe trends

- Owner: codex
- Status: DONE
- Result: added a lazy cycle-level universe panel that deduplicates the exact
  persisted Rule/Hermes symbols and renders each symbol's full available
  200-session daily trend with latest price and a cycle-selection time marker
- Checks: 310 unit tests; 4 Playwright desktop/mobile tests; scoped Ruff,
  JavaScript syntax, and Git whitespace checks
- Risks: legacy cycles without `cycle_insight.symbols` expose only their stored
  symbol count; trend lines intentionally include candles after selection while
  the amber marker identifies the universe selection point

### WEB-001 Paper cycle timeline

- Owner: codex
- Status: DONE
- Result: added a read-only `/cycles` page with live 30-second refresh,
  Rule/Hermes pairing, date/portfolio/status filters, cycle KPI/funnel views,
  and per-symbol setup-v2 block reasons; timeline API now reads a fresh database
  snapshot on every request
- Checks: 310 unit tests; 4 Playwright desktop/mobile browser tests; JavaScript
  syntax and Git whitespace checks
- Risks: old cycle rows without `cycle_insight` remain visible with aggregate
  counts only; the page does not mutate portfolios or trading state

### STRAT-007 Align Toss opening-minute timestamps

- Owner: codex
- Status: DONE
- Result: treated Toss's 09:01 completion timestamp as the 09:00-09:01
  opening minute and paged bounded older minute data for intraday symbols whose
  initial window does not include the opening bar
- Checks: 309 tests; deployed automation healthy with restart 0; 12:40 Rule and
  Hermes cycles succeeded with zero failures/API errors and zero
  `waiting:first-session-bar` skips
- Risks: no current symbol passed the strict price/flow/event gates, so the
  verified cycle correctly produced zero signals and fills

### DATA-007 Align flow coverage with dynamic universe

- Owner: codex
- Status: DONE
- Result: changed KRX import from the static symbol list to every common valid
  CSV symbol, added the latest dynamic selection to default KIS targets, and
  paged one older daily candle with Toss `nextBefore`
- Checks: 308 tests and scoped Ruff; 2,407 KRX rows for 2026-08-18 including
  all 15 current dynamic symbols; 10:45 Rule/Hermes cycles succeeded with zero
  failures, zero API count errors, and zero `199/200` skips; services restart 0
- Risks: only one first-observed flow session exists, so strict setup-v2 BUY
  remains blocked until six consecutive sessions are available; symbols missing
  from either investor CSV remain individually fail-closed

### DATA-006 Move official PIT storage to PostgreSQL partitions

- Owner: codex
- Status: DONE
- Result: moved collector and setup-v2 PIT access to `common-postgres`, added
  monthly session partitions plus idempotent SQLite migration, and retained
  SQLite fallback for local development and rollback
- Checks: 305 unit tests; scoped Ruff; exact migration parity for 12,716 events,
  25,844 universe rows, 235 flow rows, and 16 coverage rows; deployed setup-v2
  read returned the expected PostgreSQL flow observation; both services restart 0
- Risks: old SQLite remains intentionally preserved; partition archive/retention
  period is not yet set, and no automatic drop is permitted

### DATA-005 Official KRX flow CSV import

- Owner: codex
- Status: DONE
- Result: added a fail-closed manual importer for matched KRX foreign and
  institutional whole-market CSV files, immutable first-observed availability,
  source file hashes, coverage records, and KRX-over-KIS per-session precedence
- Checks: dedicated UTF-8 import, coverage, idempotency, missing-symbol, and
  duplicate-source precedence tests plus full regression suite
- Risks: downloading the two KRX CSV files remains an authenticated/manual
  operator step; no web scraping or source substitution occurs automatically

### STRAT-006 Independent setup-v2.2 lifecycle

- Owner: codex/cursor
- Status: DONE
- Result: replaced the CLI paper path's MA-generated BUY/SELL lifecycle with a
  completed-daily setup candidate, next-session gap recheck, integer risk
  sizing, persisted stop/open/cluster heat, and next-completed-minute hard-stop
  exit; unknown sectors share one conservative cluster
- Checks: 281 unit tests including shared rule/Hermes snapshot rebuild, 1m MA
  suppression, D+1 session-open validation, zero-lot rejection, persisted plan
  recovery, provisional same-cycle reservations, and stop-touch next-bar exit;
  Cursor final review blockers fixed
- Risks: KIS flow is still 0 rows locally and needs 6 observed sessions;
  `scheduled_for` remains unavailable unless OpenDART first announces it, so an
  observed undated preannouncement blocks until the realized filing; old MA
  positions have no v2 plan and intentionally fail the cycle until manually
  closed; code is not deployed and `TRADING_ENABLED=false` remains required

### DATA-004 KIS first-observed investor flow

- Owner: codex/cursor
- Status: DONE
- Result: added Korea Investment `FHPTJ04160001` collector that stores completed
  per-symbol foreign/institution net buy with first-seen `available_at`
- Checks: 257 unit tests; scoped Ruff; fake-transport TR/header parse and
  immutable first availability; live auth reached KIS and the pre-15:40 call
  was safely rejected as `OPSQ2001` without a DB write
- Risks: first full collection awaits the 18:30 run; history cannot be used
  before its first retrieval timestamp

### DATA-003 Prospective PIT event collection

- Owner: codex
- Status: DONE
- Result: added a restart-safe daily collector that refreshes the recent
  DataGo session ledger, resolves future Korean sessions through the Toss
  calendar, and checkpoints OpenDART events one date at a time; setup-v2 now
  reads covered event state and the reserved official-flow table
- Checks: 253 unit tests; scoped Ruff; live Infisical-injected OpenDART backfill
  populated 12,711 events with zero missing `available_at` values
- Risks: KRX Data Marketplace rejected unauthenticated automation with `LOGOUT`;
  per-symbol flow later moved to KIS first-observed collection (DATA-004)

### STRAT-005 Activate strict setup-v2 entry gate

- Owner: codex
- Status: DONE
- Result: every paper BUY candidate now requires 200 daily candles and strict
  setup-v2 approval before RiskManager or Hermes; missing PIT flow/events are
  recorded as `setup-v2-block`, while SELL and risk reduction remain available
- Checks: dedicated missing-input, complete-input, runtime preflight, and SELL
  bypass tests plus full regression suite; 2026-08-18 12:03 KST 배포 후 실행
  이미지의 v2 게이트·health·`TRADING_ENABLED=false` 확인
- Risks: setup-v2 currently gates MA-generated BUY candidates rather than
  generating independent entries; executable v2 integer sizing and persisted
  open/cluster heat are not connected; KIS 수급 0행이라 신규 BUY는 현재
  의도적으로 0건

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
