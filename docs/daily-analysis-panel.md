# Daily paper analysis panel

The 15:40 KST n8n workflow runs the Rule and Hermes paper closing cycles, then
queues their shared comparison snapshot in `daily_analysis_panels`. It no longer
calls the single Hermes analysis sidecar or sends the closing report itself.

The main Hermes container polls that queue with
`automation/hermes-panel-runner.py`. The script is intentionally fixed-purpose:
Cursor runs in `--mode ask`, no prompt becomes a shell command, and n8n receives
neither Docker access nor Cursor authentication.

## Rounds

1. GPT quant, Grok skeptic, and Gemini Risk analyze the same JSON independently.
2. Each model receives all three labeled independent opinions and reviews their
   agreement, conflicts, and overclaims.
3. Hermes receives the daily JSON plus all six responses and produces the final
   Telegram report.

Full model text and exact reported input, output, cache-read, and cache-write
tokens are stored in `daily_analysis_opinions`. Telegram receives only the
Hermes judgment, capped below 4000 characters; the full evidence remains in the
database.

## Hermes cron installation

The checked-in runner is the canonical source. Deployment copies it to
`/opt/data/scripts/toss-trader-daily-panel.py` in the main Hermes container's
persistent data mount, then creates one no-agent job:

```text
schedule: * 15-17 * * 1-5
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
