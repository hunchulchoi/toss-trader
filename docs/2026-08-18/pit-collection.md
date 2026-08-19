# PIT event and flow collection

## Active contract

- OpenDART events are collected idempotently from `list.json`.
- Each completed calendar date has a success checkpoint in `market_pit_coverage`.
  The current receipt date remains refreshable and is finalized only on the
  following calendar date.
- Provider resets leave completed dates intact; restart resumes at the first
  uncovered date.
- `available_at` and `blocked_through` use observed DataGo sessions plus future
  sessions verified by the Toss KR market calendar.
- The daemon runs immediately at startup and then at 00:10 and 18:30 KST. The
  00:10 run finalizes the prior OpenDART date before the next market open; the
  18:30 run captures KIS flow and refreshes same-day disclosures.
- Trading remains disabled.

## Runtime tables

- `market_events_pit_v2`: OpenDART filing facts and entry-block windows.
- `market_pit_coverage`: successful source/date coverage, including zero-event
  dates.
- `market_flow_pit_v2`: Korea Investment Open API `FHPTJ04160001` per-symbol
  investor net buy. First retrieval time is `available_at`; later polls cannot
  overwrite it.

## Flow source and PIT contract

Toss Open API still has market-index investor trading only. Per-symbol flow
comes from KIS `investor-trade-by-stock-daily`. Rows after `completed_through`
are dropped so an incomplete session is not stored as if it were final.
Naver values are not promoted.

- `KIS_APP_KEY` and `KIS_APP_SECRET` are injected and never logged or stored.
- KIS opens this TR after 15:40 KST. The 18:30 daemon run is inside that
  window; an earlier service start reports `WAITING_FOR_KIS_1540` without an
  API call.
- Net-buy amounts use `frgn_ntby_tr_pbmn` and `orgn_ntby_tr_pbmn`, with
  `acml_tr_pbmn` as the ratio denominator.
- KRX short codes may be six uppercase alphanumeric characters; codes such as
  `0004V0` are valid common stocks, not malformed numeric symbols. Historical
  rows with blank amount fields are ignored, while malformed nonblank amounts
  fail only that symbol and do not terminate the daemon.
- If the DataGo price ledger lags the completed KIS date, session indexes are
  extended only across business days confirmed by the Toss KR calendar. This
  prevents valid recent KIS rows from being discarded while keeping holidays
  fail-closed.
- History returned by the first call is usable only after its actual retrieval
  timestamp. It is never backdated for a historical decision.
- Each symbol commits independently and first observations are immutable, so a
  later failure can resume without losing earlier progress.
- The default KIS target is the union of `market_symbols` and the latest
  successful dynamic-universe selection. A changing intraday universe is not
  limited to the original static symbol list.

Six completed PIT sessions are still required by setup-v2. First-observed
collection starting 2026-08-18 makes the first strict window available at the
2026-08-26 open, assuming no intervening closure.

## Verification snapshot

On 2026-08-18 an Infisical-injected local run collected:

- 12,711 unique OpenDART event receipts covering 2026-08-04 through 2026-08-18
- 611 entry-blocking reports
- zero rows missing `available_at`
- KIS credentials authenticated at 11:45 KST; the flow TR returned `OPSQ2001`
  before its 15:40 availability boundary, so no row was written
- 11:54 KST 새 컨테이너 배포 후 KIS 키 존재와 `TRADING_ENABLED=false`를
  확인했다. 검증된 200종목 명단을 수집 DB에 동기화했으며 시작 결과는
  `WAITING_FOR_KIS_1540`; 다음 정규 실행은 18:30 KST다.
