# Midday and closing paper analysis panel

The n8n workflow runs at 11:50 and 15:40 KST on Korean market days, then queues
the Rule and Hermes shared comparison snapshot in `daily_analysis_panels`. Both
schedules pass the Toss market-calendar gate first. Manual and authenticated
webhook runs use the same gate, so no briefing runs on a closed market day. The
11:50 context is marked `midday` and non-final; 15:40 is marked `close`. The
workflow no longer calls the single Hermes analysis sidecar or sends the report
itself.

The main Hermes container polls that queue with
`automation/hermes-panel-runner.py`. The script is intentionally fixed-purpose:
Cursor runs in `--mode ask` from a disposable empty workspace, no prompt becomes
a shell command, and n8n receives neither Docker access nor Cursor
authentication. Each Cursor subprocess gets one ephemeral MCP server:
`toss-panel`, whose `/panel-mcp` endpoint exposes only the cutoff evidence tool.

## Rounds

1. Grok quant, Grok skeptic, and Gemini Risk analyze the same JSON independently.
2. Each model receives all three labeled independent opinions and reviews their
   agreement, conflicts, and overclaims.
3. Hermes receives the briefing JSON plus all six responses and produces the
   Telegram report. Midday output must not claim a closing price or final daily
   performance.

## Evidence research

JSON is still the primary evidence. When a fill timestamp, ledger cash, full
reason transition, Risk decision, D-1 bar, or key 1m bar is omitted or conflicts,
an agent may call `toss_paper_panel_evidence` at most twice. The fixed tool accepts
only the claimed `panelId`, `session-summary`, or up to ten exact symbols for
`symbol-trace`. PostgreSQL is session read-only and every query is bounded by the
panel's stored `briefing.observedAt`; no arbitrary SQL or order path exists.
The Hermes judge runs with explicit toolsets `web,toss-panel`; terminal, file,
code execution, Grafana, the current-status `toss-paper` tools, and plugins are
not in that invocation.

Public research is limited to KRX, KIS Developers, OpenDART, and the Korean public
data portal. The opinion must include the official URL and publication/observation
time. Anything published after the panel cutoff is labeled
`post-cutoff-research` and cannot become a historical trading input or proof of a
missed trade. Every opinion distinguishes panel omission, absent source data, and
facts it did not search. `missing-price-setup` remains a normal pattern rejection,
not missing price data.

The 1d cycle attaches `marketContext` from stored KR 1m bars (benchmark plus
watched symbols) and fetches missing session minutes for those symbols first.
`entryWindow` is the actual D+1 arm clock. `universe.cacheHit` means same-day
freeze, not a missing run. `intradaySample.applicable=false` on the 1d cycle is
expected; use `intradayReview.reasonPath` and `armRejectDetail` for
`below-one-lot`. The panel must contrast skip reasons with those facts. It must
not invent missed buys from news, and must not treat zero fills as strategy
proof.

Full model text and exact reported input, output, cache-read, and cache-write
tokens are stored in `daily_analysis_opinions`. Telegram receives only the
Hermes judgment, capped below 4000 characters; the full evidence remains in the
database.

## Hourly market watch

`toss-trader-hourly-watch` runs at 10:03 through 15:03 KST on Korean market
days. It reads the Rule and Hermes daily snapshots without placing an order,
then deterministically checks stored session bars and reason transitions for:

- cycle/API/data failures and missing one-minute coverage;
- benchmark moves or divergence;
- watched symbols that had no BUY and later outperformed enough to require a
  hindsight review.

Every check is written to `automation_run_logs` as `hourly_market_watch` and is
shown in the timeline Hermes conversation view. An identical anomaly is
suppressed; no-anomaly checks remain visible but do not send Telegram. A new or
changed anomaly queues one Hermes judge, whose full opinion and token usage are
stored in `daily_analysis_opinions`. Only that final judgment is sent to
Telegram. The judge has the same `web,toss-panel` boundary and official-source,
cutoff, and post-cutoff rules described above. It cannot change a gate or place
an order.

The Hermes conversation page opens on the current Seoul date, including a
current-date option when there are no rows yet. Users may explicitly select
`전체 날짜` for historical conversations.

## Hermes cron installation

The checked-in runner is the canonical source. Deployment copies it to
`/opt/data/scripts/toss-trader-daily-panel.py` in the main Hermes container's
persistent data mount, then creates three no-agent jobs:

```text
hourly schedule: 5-19 1-5 * * 1-5 (UTC = 10:05-14:19 KST)
midday schedule: 50-59 2 * * 1-5 (UTC = 11:50-11:59 KST)
closing schedule: * 6-8 * * 1-5 (UTC = 15:00-17:59 KST)
script: toss-trader-daily-panel.py
mode: --no-agent
delivery: local
```

The three polls use the same idempotent claim endpoint and are silent when no
job exists. The closing poll also consumes the 15:03 hourly queue, avoiding an
overlapping fourth poll window. A model or persistence failure marks the
panel failed and sends the existing critical workflow alert. `TRADING_ENABLED`
is unrelated and remains `false`; the panel cannot place orders.

## Operational checks

- n8n queue response contains `queued=true` and a `panelId`.
- `daily_analysis_panels.status` reaches `succeeded`.
- exactly seven unique opinion stages exist for the panel.
- every stage has non-negative provider token fields.
- the final `judge:hermes` content matches the Telegram report.
- tool-backed claims name the MCP topic or official URL and preserve the panel
  cutoff.
