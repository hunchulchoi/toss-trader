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

No tasks awaiting review.

## DONE

### DATA-012 Retry transient DNS and network errors on Toss Open API

- Owner: codex
- Status: DONE
- Result: added 1-retry fallback (`max_retries=1`, 0.5s backoff) for transient DNS
  resolution failures (`[Errno -2] Name or service not known`), socket timeouts, and
  OS network errors in `UrllibTransport`
- Checks: 390 unit tests; scoped Ruff and Git whitespace; live container build and deployment
- Risks: none; idempotent GET/token requests with bounded 1-retry
- 기록 시각: 2026-08-24 14:01 KST

### STRAT-017 Give Hermes a paper-only risk budget

- Owner: codex
- Status: DONE
- Result: preserved Rule sizing at 0.5% per trade, 2% open heat, and 1%
  UNKNOWN-cluster heat while applying 2%/6%/6% only to Hermes experimental
  entries through an explicit `arm_candidate` sizing policy
- Checks: 377 unit tests; changed-file Ruff and Git whitespace; one-share fixture
  with planned loss above KRW 5,000 and at or below KRW 20,000 rejected by Rule
  sizing and accepted by Hermes sizing
- Risks: one-share planned loss above KRW 20,000 still yields zero quantity;
  paper-only, asymmetric strategy experiment, not deployed or profitability evidence
- 기록 시각: 2026-08-24 12:38 KST

### STRAT-016 Split Hermes into a reference-gate paper strategy

- Owner: codex
- Status: DONE
- Result: kept Rule strict while giving Hermes a selected-first, static-eligible
  D-1 liquidity pool of up to 30 names; only `missing-price-setup`, `rsi-chase`,
  and `falling-knife` became advisor evidence, with all Hermes entries labeled
  `hermes-experimental`
- Checks: 376 unit tests; changed-file Ruff and Git whitespace checks; explicit
  coverage for cache reload, price-strategy relaxation, mandatory data/event
  rejection, unknown future gate fail-closed, plan labeling, and no Rule changes
- Risks: candidate and strategy denominators differ, so Rule/Hermes is not direct
  A/B evidence; unchanged integer-share 0.5% sizing can still yield zero Hermes
  signals; this is paper-only and has not been deployed
- 기록 시각: 2026-08-24 12:29 KST

### STRAT-015 Expand paper entry observation without loosening setup gates

- Owner: codex
- Status: DONE
- Result: kept paper capital at KRW 1,000,000 and setup-v2.3 risk rates,
  raised only the per-order notional ceiling to KRW 700,000, allowed entries
  through 09:30 KST, and recorded later armable cases as shadow-only reasons
- Checks: 374 unit tests; changed-file Ruff and Git whitespace checks; direct
  production-data sizing diagnostic for the two 2026-08-24 candidates
- Risks: the unchanged 0.5% per-trade risk budget is KRW 5,000, so high-priced
  volatile candidates can still produce `below-one-lot`; shadow signals never
  call RiskManager or Hermes and never create orders or fills
- 기록 시각: 2026-08-24 11:35 KST

### WEB-004 Connect midday and close panel briefings to Hermes conversation view

- Owner: codex
- Status: DONE
- Result: queried `daily_analysis_panels` and `daily_analysis_opinions` in
  `PostgresPaperTimelineStore`, integrated 11:50 midday (`midday`) and 15:40 close
  (`daily`) panel synthesis verdicts (`judge:hermes`) into `/hermes` conversation
  feed, and added `중간 분석` (`midday`) to kind filters
- Checks: 372 unit tests; scoped Ruff and Git whitespace; live container smoke check
  confirming 71 Hermes conversations loaded with midday and close briefings
- Risks: none; read-only query on PostgreSQL panels
- 기록 시각: 2026-08-24 10:38 KST

### DATA-004 Restore a weekly setup-v2 price sample

- Owner: codex
- Status: DONE
- Result: fixed historical intraday cursor seeding and legacy candidate lookup,
  restored 73,479 1m candles plus 10,900 daily warmup candles,
  and completed 46 price-setup symbol-days with full regular-session 1m coverage
- Checks: 372 unit tests, scoped Ruff, Git whitespace, zero Toss failures across
  four session backfills, and a repeatable read-only diagnostic over 357 current
  static-eligible symbols
- Risks: counterfactual price-only sample with current-pool/static survivorship
  bias; not exact replay, strict setup-v2 approval, PnL, or profitability evidence
- 기록 시각: 2026-08-21 17:00 KST

### STRAT-014 Build a setup-first opening pool and useful midday review

- Owner: codex/cursor/agy
- Status: DONE
- Result: observe KRX D-1 at 08:35, evaluate the PIT-managed pool through
  current D-1 candles and mandatory PIT/event gates before liquidity ranking,
  preserve truthful opening reasons, and provide one bounded transition-based
  midday panel snapshot with explicit briefing kinds
- Checks: 365 unit tests, changed-file Ruff, Git whitespace, n8n JSON and new
  Schedule/Set node schema validation, independent Cursor and agy review
- Risks: first production 08:35 KRX response and n8n published graph still need
  rollout smoke; actual paper signal frequency and profitability remain unknown
- 기록 시각: 2026-08-21 14:40 KST

### STRAT-013 Give Hermes the expanded Top30 candidate pool

- Owner: codex
- Status: DONE
- Result: Rule keeps the selected Top15 snapshot. Hermes receives a separate
  static-eligible Top30 snapshot, rebuilds setup-v2.3 candidates from the shared
  stored market/PIT data, and keeps out-of-pool holdings for SELL management.
- Checks: expanded snapshot selection, sample-error isolation, held-symbol union,
  legacy process compatibility, automation/cycle tests, and full unit suite
- Risks: Rule and Hermes no longer share the same candidate denominator, so the
  output is labeled as asymmetric pool research rather than a direct A/B test.
  All deterministic setup, event, gap, timing, and execution Risk gates remain.

### DATA-003 Expand intraday samples beyond the trading universe

- Owner: codex
- Status: DONE
- Result: trading membership remains capped at 15 symbols, while the latest
  static-eligible Top30 is preserved as the collection pool. Each 1m rule cycle
  upserts 30 recent bars for non-trading candidates; cycle symbols retain their
  existing strategy-sized collection. A bounded backfill command restores all
  available session bars for the observed candidate union and reports per-symbol
  page/completion/restoration counts.
- Checks: universe/store cache coverage, background sampling failure isolation,
  scoped Ruff, and full unit suite
- Risks: forward collection only; historical gaps remain. Extra candidates add
  at most 15 Toss candle requests per cycle under the current 15/30 limits.

### STRAT-012 Make observed flow reversal a setup-v2.3 bonus

- Owner: codex
- Status: DONE
- Result: six consecutive PIT flow sessions remain mandatory, while a missing
  foreign reversal no longer vetoes an otherwise valid price setup. Foreign and
  institutional confirmation remain ranking evidence through flow stars. New
  entry arming is limited to the first ten minutes after market open, while
  exits for existing plans remain active all session.
- Checks: setup, market scan, cycle, automation, and full unit suites
- Risks: this deliberately increases paper entry frequency. Midday rollout no
  longer backfills an opening-bar entry, but the next full session remains the
  first representative paper observation.

### STRAT-011 Afternoon KRX prior-session amount rankings

- Owner: cursor
- Status: DONE
- Result: Seoul 12:00+ universe ranking uses KRX prior-session `ACC_TRDVAL`
  (KOSPI+KOSDAQ). Morning still Toss realtime. Afternoon cache requires
  `ranking_source=krx:acc-trdval` so the 10:25 Toss freeze is not reused.
  Fail-closed on missing key, 401, or empty `OutBlock_1`.
- Checks: krx_openapi, calendar, universe, config, compose asset tests
- Risks: KRX has no same-day amount; afternoon names can differ from the
  morning 15. No per-symbol investor flow on this API.

### STRAT-010 Split universe membership from price setup

- Owner: cursor
- Status: DONE
- Result: eligible names with 200 completed daily bars fill up to 15 slots.
  Price setup stays a BUY gate. A successful 0-stock run is not reused as the
  Seoul-day freeze, so the next cycle retries.
- Checks: universe unit tests
- Risks: morning 2026-08-20 cycles stay empty until automation redeploy; later
  membership uses later realtime rankings than 09:00

### AI-004 Beginner-friendly premarket briefing semantics

- Owner: codex
- Status: DONE
- Result: aligned the direct and n8n Hermes premarket prompts; briefings now lead
  with a plain-language conclusion and distinguish actual missing data or errors
  from valid price-setup, flow-confirmation, and imminent-event rejections
- Checks: 339 unit tests; scoped Ruff, JSON, and Git whitespace checks
- Risks: free-form LLM wording can still vary, but the prompt now defines each
  machine reason explicitly and forbids describing normal rejections as missing data

### AI-003 Add a market-day midday paper briefing

- Owner: codex
- Status: DONE
- Result: the shared daily panel now runs at 11:50 and 15:40 KST. Scheduled,
  manual, and authenticated webhook entry points all pass the Toss Korean-market
  calendar gate. The 11:50 snapshot is labeled non-final in DB context, model
  prompts, and Telegram output so no analyst can describe it as a closing result.
  Separate Hermes no-agent poll windows consume the midday and closing queues
  promptly through the same idempotent claim endpoint.
- Checks: 338 unit tests; Ruff, JSON, Git whitespace, and live n8n node validation;
  published workflow export, services, safety switch, queue, and runner dry smoke
- Risks: each briefing invokes the full seven-opinion panel, so weekday model
  usage increases by one panel. Calendar lookup failure remains fail-closed.

### AI-002 Multi-agent daily paper review panel

- Owner: codex
- Status: DONE
- Result: n8n queues the shared closing snapshot; main Hermes runs GPT quant,
  Grok 4.6 Fast skeptic, and Gemini 3.7 Flash Risk independent opinions and
  cross-reviews before a Hermes final judge. Seven opinions and provider token
  counts are idempotently stored before the final Telegram report.
- Checks: 336 unit tests; scoped Ruff and Git whitespace; real read-only model
  JSON/token smoke for all four models; production schema, n8n graph, empty
  queue runner, health, restart count, and `TRADING_ENABLED=false`; full manual
  seven-stage panel succeeded with seven DB opinions and final reporter accepted
- Risks: Cursor/Hermes provider latency or failure suppresses the final report
  and sends a critical failure alert while preserving completed opinions. The
  first automatically scheduled run remains the next 15:40 KST closing cycle.

### AI-001 Pass cycle candles and setup summary to Hermes trade advisor

- Owner: codex/cursor
- Status: DONE
- Result: included recent 30 completed daily candles, 60 1m candles, setup-v2 price
  condition, and 6-session PIT flow summary in the Hermes trade advisor user payload;
  strengthened system prompt against approving solely on risk limits without strategy rationale
- Checks: 332 unit tests; scoped Ruff and Git whitespace checks
- Risks: increased prompt payload size; advisor remains restricted to provided snapshot

### WEB-003 Hermes audit replies page

- Owner: codex
- Status: DONE
- Result: added `/hermes` web page that reads `automation_run_logs` for `market_scan`,
  `daily`, and `hermes_trade`, displaying assistant response text (up to 4000 chars)
  and run metadata without exposing secrets or raw request payloads
- Checks: unit tests; live deployment check (`/hermes` 200, 58 conversations loaded)
- Risks: legacy runs before this change recorded token counts only without assistant text

### DOCS-001 Official terminology glossary

- Owner: agy/codex
- Status: DONE
- Result: created canonical `docs/glossary.md` covering system safety, PIT integrity,
  setup-v2.2 strategy rules, ledger/PnL engine, automation, and multi-agent roles;
  linked from `README.md` and `docs/automatic-trading-scenario.md`
- Checks: cross-document link validation and unit test suite
- Risks: none (documentation artifact)

### DATA-009 Finalize OpenDART coverage after the receipt date

- Owner: codex
- Status: DONE
- Result: keep the current receipt date refreshable, ignore checkpoints completed
  on or before their coverage date, and add a 00:10 KST finalization run before
  the next market open while retaining the 18:30 KIS/event refresh
- Checks: 332 unit tests; scoped Ruff and Git whitespace; live comparison found
  329 OpenDART receipts versus 4 prematurely checkpointed database rows;
  production refresh raised the stored date to 345 rows with zero collector errors
- Risks: same-day DB count may trail filings added after the latest refresh until
  the 18:30 refresh; final coverage is intentionally written at 00:10 next day

### DATA-008 Extend KIS sessions past the lagging price ledger

- Owner: codex
- Status: DONE
- Result: extend the repository session index through the KIS completion date
  only with Toss-confirmed Korean sessions, so delayed DataGo price rows no
  longer discard newer KIS flow; accept six-character alphanumeric KRX stock
  codes and isolate blank/malformed history without stopping the daemon
- Checks: 330 unit tests; scoped Ruff and Git whitespace; live KIS/Toss smoke
  stored 2026-08-14, 2026-08-18, and 2026-08-19 as consecutive temporary rows
  with zero failures; live `0004V0` returned 30 rows through 2026-08-19
- Risks: KIS first-observed history remains unavailable before its actual
  retrieval time; no backdating is permitted

### STRAT-009 Harden setup-v2 universe membership

- Owner: codex/cursor/agy
- Status: DONE
- Result: amount-only overfetch, static eligible reranking, setup filtering, valid
  empty caching, acquisition failure retry, mutable Risk execution-only, and
  local membership/n8n execution Risk separation. Raw/eligible ranks are audit
  provenance, not exact replay inputs.
- Checks: 327 unit tests; scoped Ruff; JSON and n8n Code syntax; Git whitespace;
  isolated real Toss smoke; Cursor and agy reviews with zero MUST FIX findings;
  production health/restart/safety switch and PostgreSQL migration verified
- Risks: today's pre-deployment successful universe remains cached by contract;
  the first live selection under the new rules occurs next Seoul trading day.
  Exact replay and full-market recall remain unsupported.

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
