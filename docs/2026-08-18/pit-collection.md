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
- `market_flow_pit_v2`: strict schema reserved for an authorized official
  per-symbol investor-flow source.

## Flow limitation

Toss Open API exposes investor trading for KOSPI/KOSDAQ market indicators, not
individual equities. The KRX Data Marketplace contains per-symbol investor
statistics, but its unauthenticated automation endpoint returned `LOGOUT`.
Therefore flow remains `UNKNOWN_NO_AUTHORIZED_SOURCE`; Naver values and guessed
publication timestamps are not promoted into the strict table.

Once an authorized source is configured, six completed sessions are required by
setup-v2. Starting on 2026-08-18 would make the first strict six-session window
available at the 2026-08-26 open, assuming no intervening closure.

## Verification snapshot

On 2026-08-18 an Infisical-injected local run collected:

- 12,711 unique OpenDART event receipts covering 2026-08-04 through 2026-08-18
- 611 entry-blocking reports
- zero rows missing `available_at`
- zero official flow rows, intentionally fail-closed
