# PIT event and flow collection

## Active contract

- OpenDART events are collected idempotently from `list.json`.
- Each calendar date has a success checkpoint in `market_pit_coverage`.
- Provider resets leave completed dates intact; restart resumes at the first
  uncovered date.
- `available_at` and `blocked_through` use observed DataGo sessions plus future
  sessions verified by the Toss KR market calendar.
- The daemon runs immediately at startup and then daily at 18:30 KST.
- Trading remains disabled.

## Runtime tables

- `market_events_pit_v2`: OpenDART filing facts and entry-block windows.
- `market_pit_coverage`: successful source/date coverage, including zero-event
  dates.
- `market_flow_pit_v2`: Korea Investment Open API `FHPTJ04160001` per-symbol
  investor net buy. First retrieval time is `available_at`; later polls cannot
  overwrite it.

## Flow source

Toss Open API still has market-index investor trading only. Per-symbol flow
comes from KIS `investor-trade-by-stock-daily`. Rows after `completed_through`
are dropped so an incomplete session is not stored as if it were final.
Naver values are not promoted.

Six completed PIT sessions are still required by setup-v2. First-observed
collection starting 2026-08-18 makes the first strict window available at the
2026-08-26 open, assuming no intervening closure.

## Verification snapshot

On 2026-08-18 an Infisical-injected local run collected:

- 12,711 unique OpenDART event receipts covering 2026-08-04 through 2026-08-18
- 611 entry-blocking reports
- zero rows missing `available_at`
- KIS per-symbol flow collector added after this snapshot; live row counts
  depend on a later `collect-kis-flow` or pit-collector run with Infisical keys
