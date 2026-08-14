# Official PIT dataset rebuild

## Scope

Codex implemented the connector and local SQLite pipeline. Cursor independently
reviewed the diff twice. agy was intentionally excluded to preserve its token
budget.

The earlier `market_pit_valuations`, `market_pit_events`, and
`market_universe_raw` tables remain invalid drafts. The new pipeline writes only
versioned v2 tables and does not connect to live trading.

## Corrections

- OpenDART full-account data stores both CFS and OFS with `rcept_no`,
  `account_id`, statement division, source, retrieval time, and payload hash.
- Valuation snapshots prefer CFS, compute rolling TTM EPS from cumulative
  reports, and derive BPS from parent-owner equity per listed share. No growth
  or percentile is hardcoded.
- Filing availability uses the next observed Korean market session at 08:00
  KST. The 2025-08-14 filing sample resolves to 2025-08-18, not the Liberation
  Day holiday.
- Unscheduled disclosures never block before disclosure. They block only for
  two sessions beginning at conservative availability; no D-2 inference is
  generated from `list.json`.
- DataGo rows are paginated and committed page by page. Their archive is treated
  as published on the next observed session at 13:00 and usable from the
  following session. Incremental reruns recompute boundaries using all stored
  sessions.

## Verification

- Full-market raw v2: 1,387,301 rows, 483 sessions, 3,049 symbols,
  2024-08-16 through 2026-08-13.
- Availability precedes publication: 0 rows.
- Samsung Electronics sample stores CFS EPS 6,605 and OFS EPS 5,027 separately.
- The 2025 annual CFS snapshot calculates TTM EPS 6,605 and YoY growth
  0.334343..., with derived BPS 71,678.92 using parent-owner equity per listed
  share.
- Python unit suite: 242 passed. Scoped Ruff: passed.
- Cursor pagination review passed; its later full review found a correction-filing
  as-of bug, which was fixed by cutting every TTM input at snapshot
  `available_at` and adding a regression test.
- OpenDART backfill checkpoint: 87,715 account facts across 31 symbols. The
  provider then repeatedly reset connections; the remaining 169-symbol backfill
  is incomplete and must be resumed with the idempotent command.
- The v2 event connector passed unit tests, but the live event backfill did not
  start because the same OpenDART network reset persisted.

## Remaining fail-closed items

- The DataGo issuance/master endpoint returns HTTP 403 for the current service
  subscription. `security_type` therefore remains `UNKNOWN`; common-stock TopN
  generation is prohibited until the official master endpoint is authorized.
- Forward consensus remains unavailable. Strategy confidence multiplier stays
  at x1.0; trailing valuation cannot be relabeled as forward consensus.
- Raw v2 tables and snapshots are not wired to order execution.
- Financial/event completeness gates remain closed until OpenDART backfill
  finishes; partial rows must not be treated as a complete cross-section.
