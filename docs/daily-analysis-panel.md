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
Cursor runs in `--mode ask`, no prompt becomes a shell command, and n8n receives
neither Docker access nor Cursor authentication.

## Rounds

1. GPT quant, Grok skeptic, and Gemini Risk analyze the same JSON independently.
2. Each model receives all three labeled independent opinions and reviews their
   agreement, conflicts, and overclaims.
3. Hermes receives the briefing JSON plus all six responses and produces the
   Telegram report. Midday output must not claim a closing price or final daily
   performance.

Full model text and exact reported input, output, cache-read, and cache-write
tokens are stored in `daily_analysis_opinions`. Telegram receives only the
Hermes judgment, capped below 4000 characters; the full evidence remains in the
database.

## Hermes cron installation

The checked-in runner is the canonical source. Deployment copies it to
`/opt/data/scripts/toss-trader-daily-panel.py` in the main Hermes container's
persistent data mount, then creates one no-agent job:

```text
schedule: * 6-8 * * 1-5 (UTC = 15:00-17:59 KST)
script: toss-trader-daily-panel.py
mode: --no-agent
delivery: local
```

The poll is silent when no job exists. A model or persistence failure marks the
panel failed and sends the existing critical workflow alert. `TRADING_ENABLED`
is unrelated and remains `false`; the panel cannot place orders.

## Operational checks

- n8n queue response contains `queued=true` and a `panelId`.
- `daily_analysis_panels.status` reaches `succeeded`.
- exactly seven unique opinion stages exist for the panel.
- every stage has non-negative provider token fields.
- the final `judge:hermes` content matches the Telegram report.
