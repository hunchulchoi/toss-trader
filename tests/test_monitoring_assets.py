import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dashboard_panels(dashboard: dict) -> list[dict]:
    panels: list[dict] = []
    for panel in dashboard["panels"]:
        if panel.get("type") == "row":
            panels.extend(panel.get("panels") or [])
        else:
            panels.append(panel)
    return panels


class MonitoringAssetsTest(unittest.TestCase):
    def test_timeline_is_tailscale_only_and_select_only(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        block = compose.split("  timeline:\n", 1)[1].split(
            "\n  hermes-analysis:\n", 1
        )[0]

        self.assertIn('command:\n      - serve-paper-timeline', block)
        self.assertIn("${TIMELINE_PORT:-19094}:8091", block)
        self.assertIn("TOSS_MCP_POSTGRES_USER", block)
        self.assertIn("TOSS_MCP_POSTGRES_PASSWORD", block)
        self.assertIn("read_only: true", block)
        self.assertIn("cap_drop:\n      - ALL", block)
        self.assertNotIn("TOSS_CLIENT", block)
        self.assertNotIn("TOSS_ACCOUNT", block)

    def test_grafana_dashboard_queries_exported_metrics(self) -> None:
        dashboard_path = (
            ROOT / "monitoring" / "grafana" / "dashboards" / "toss-trader.json"
        )
        dashboard = json.loads(dashboard_path.read_text())
        panels = _dashboard_panels(dashboard)
        expressions = {
            target["expr"]
            for panel in panels
            for target in panel.get("targets", [])
            if "expr" in target
        }
        sql_queries = {
            target["rawSql"]
            for panel in panels
            for target in panel.get("targets", [])
            if "rawSql" in target
        }

        self.assertEqual(dashboard["uid"], "toss-trader")
        self.assertGreaterEqual(len(dashboard["panels"]), 8)
        self.assertIn("toss_trader_cycle_last_success", expressions)
        self.assertIn("toss_trader_cycle_last_daily_return_ratio", expressions)
        self.assertIn("toss_trader_paper_initial_cash_krw", expressions)
        self.assertIn("toss_trader_paper_available_cash_krw", expressions)
        self.assertIn("toss_trader_paper_deployed_cash_krw", expressions)

        datasources = {
            panel["datasource"]["uid"]
            for panel in panels
            if "datasource" in panel
        }
        self.assertEqual(datasources, {"toss-prometheus", "toss-postgres"})
        self.assertGreaterEqual(
            sum(1 for panel in dashboard["panels"] if panel.get("type") == "row"), 3
        )
        self.assertTrue(any("paper_risk_decisions" in query for query in sql_queries))
        self.assertTrue(any("automation_run_logs" in query for query in sql_queries))
        self.assertTrue(any("paper_cycle_runs" in query for query in sql_queries))
        self.assertTrue(any("paper_fills" in query for query in sql_queries))
        self.assertTrue(any("dynamic_universe_runs" in query for query in sql_queries))
        self.assertTrue(
            any("dynamic_universe_decisions" in query for query in sql_queries)
        )
        self.assertTrue(any("prompt_tokens" in query for query in sql_queries))
        titles = {panel["title"] for panel in panels}
        self.assertNotIn("Rule vs Hermes Daily Return", titles)
        self.assertNotIn("Rule vs Hermes Available Cash", titles)
        equity_panel = next(
            panel
            for panel in panels
            if panel["title"] == "Rule vs Hermes 평가금액 · 손익"
        )
        self.assertEqual(equity_panel["gridPos"]["w"], 24)
        self.assertEqual(equity_panel["fieldConfig"]["defaults"]["unit"], "currencyKRW")
        equity_sql = equity_panel["targets"][0]["rawSql"]
        self.assertIn("평가금액", equity_sql)
        self.assertIn("손익", equity_sql)
        self.assertIn("실현손익", equity_sql)
        self.assertIn("미실현손익", equity_sql)
        self.assertIn("paper_portfolio_snapshots", equity_sql)
        self.assertIn("initial_cash", equity_sql)
        self.assertNotIn("market_candles", equity_sql)
        self.assertNotIn("daily_return_rate * 100", equity_sql)
        rule_trades = next(panel for panel in panels if panel["title"] == "Rule Trades (1m)")
        hermes_trades = next(
            panel for panel in panels if panel["title"] == "Hermes Trades (1m)"
        )
        for panel, portfolio in ((rule_trades, "rule"), (hermes_trades, "hermes")):
            other = "hermes" if portfolio == "rule" else "rule"
            self.assertEqual(panel["type"], "timeseries")
            self.assertEqual(len(panel["targets"]), 2)
            self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "percent")
            self.assertIn("market_candles", panel["targets"][0]["rawSql"])
            self.assertIn("interval = '1m'", panel["targets"][0]["rawSql"])
            self.assertIn(f"portfolio_id = '{portfolio}'", panel["targets"][0]["rawSql"])
            self.assertNotIn(
                f"portfolio_id = '{other}'", panel["targets"][0]["rawSql"]
            )
            self.assertIn("paper_fills", panel["targets"][1]["rawSql"])
            self.assertIn(f"portfolio_id = '{portfolio}'", panel["targets"][1]["rawSql"])
            self.assertNotIn(
                f"portfolio_id = '{other}'", panel["targets"][1]["rawSql"]
            )
            self.assertIn("f.side", panel["targets"][1]["rawSql"])
            self.assertIn("${trade_symbol:sqlstring}", panel["targets"][0]["rawSql"])
        self.assertEqual(rule_trades["gridPos"]["w"], 12)
        self.assertEqual(hermes_trades["gridPos"]["x"], 12)
        self.assertIn("Paper Cycle Run Log", titles)
        cycle_panel = next(
            panel for panel in panels if panel["title"] == "Paper Cycle Run Log"
        )
        self.assertEqual(cycle_panel["type"], "timeseries")
        self.assertEqual(cycle_panel["targets"][0]["format"], "time_series")
        self.assertIn("paper_portfolios", cycle_panel["targets"][0]["rawSql"])
        self.assertIn(
            "portfolio_id IN ('rule', 'hermes')", cycle_panel["targets"][0]["rawSql"]
        )
        symbol_panel = next(
            panel for panel in panels if panel["title"].startswith("Symbols (1m")
        )
        self.assertEqual(symbol_panel["type"], "timeseries")
        self.assertEqual(symbol_panel["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertIn("market_candles", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("interval = '1m'", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("market_symbols", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("display_name", symbol_panel["targets"][0]["rawSql"])
        self.assertEqual(len(symbol_panel["targets"]), 2)
        self.assertIn("paper_fills", symbol_panel["targets"][1]["rawSql"])
        self.assertIn("f.executed_at AS time", symbol_panel["targets"][1]["rawSql"])
        self.assertIn("${trade_symbol:sqlstring}", symbol_panel["targets"][0]["rawSql"])
        self.assertNotIn("CASE symbol", symbol_panel["targets"][0]["rawSql"])
        trade_variable = next(
            variable
            for variable in dashboard["templating"]["list"]
            if variable["name"] == "trade_symbol"
        )
        self.assertIn(
            "SELECT DISTINCT symbol FROM market_candles", trade_variable["query"]
        )
        self.assertIn("EXISTS (SELECT 1 FROM paper_fills", trade_variable["query"])
        self.assertEqual(trade_variable["label"], "종목")
        trade_filter = next(
            variable
            for variable in dashboard["templating"]["list"]
            if variable["name"] == "trade_filter"
        )
        self.assertEqual(trade_filter["label"], "종목 범위")
        self.assertEqual(trade_filter["current"]["value"], "traded")
        self.assertIn("전체 조회 종목 : all", trade_filter["query"])
        self.assertIn("Recent Paper Fills", titles)
        self.assertIn("Dynamic Universe Risk Decisions", titles)
        positions_panel = next(
            panel for panel in panels if panel["title"] == "Open Paper Positions"
        )
        self.assertIn("market_symbols", positions_panel["targets"][0]["rawSql"])
        self.assertIn("display_name", positions_panel["targets"][0]["rawSql"])
        risk_panel = next(
            panel for panel in panels if panel["title"] == "Recent RiskManager Decisions"
        )
        self.assertIn("market_symbols", risk_panel["targets"][0]["rawSql"])
        self.assertIn("jsonb_array_elements_text", risk_panel["targets"][0]["rawSql"])
        self.assertIn('AS "판단 근거"', risk_panel["targets"][0]["rawSql"])
        fills_panel = next(
            panel for panel in panels if panel["title"] == "Recent Paper Fills"
        )
        self.assertIn("market_symbols", fills_panel["targets"][0]["rawSql"])
        self.assertIn("f.reason", fills_panel["targets"][0]["rawSql"])
        self.assertIn("WITH RECURSIVE", fills_panel["targets"][0]["rawSql"])
        self.assertIn("commission", fills_panel["targets"][0]["rawSql"])
        self.assertIn("fill_sequence", fills_panel["targets"][0]["rawSql"])
        self.assertIn("평균원가", fills_panel["targets"][0]["rawSql"])
        self.assertIn("매수시각", fills_panel["targets"][0]["rawSql"])
        self.assertIn("실현손익", fills_panel["targets"][0]["rawSql"])
        self.assertIn('"수수료"', fills_panel["targets"][0]["rawSql"])
        self.assertIn('"세금"', fills_panel["targets"][0]["rawSql"])
        self.assertIn("Hermes Automation Run Log", titles)
        self.assertIn("n8n Flow Review Log", titles)
        hermes_log = next(
            panel for panel in panels if panel["title"] == "Hermes Automation Run Log"
        )
        self.assertIn("hermes_trade", hermes_log["targets"][0]["rawSql"])
        self.assertIn("market_scan", hermes_log["targets"][0]["rawSql"])
        self.assertNotIn("details::text", hermes_log["targets"][0]["rawSql"])
        self.assertNotIn("DS_PROMETHEUS", dashboard)

    def test_prometheus_assets_cover_outage_loss_and_stale_cycle(self) -> None:
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text()
        scrape = (ROOT / "monitoring" / "prometheus" / "scrape-job.yml").read_text()

        self.assertIn("TossTraderMetricsDown", alerts)
        self.assertIn("toss_trader_cycle_last_daily_return_ratio <= -0.03", alerts)
        self.assertIn("toss_trader_cycle_last_finished_timestamp_seconds", alerts)
        self.assertIn("job_name: toss-trader", scrape)
        self.assertIn("/metrics", scrape)
        self.assertIn("alertmanager:9093", scrape)

    def test_grafana_provisioning_points_to_dashboard_directory(self) -> None:
        provisioning = (
            ROOT
            / "monitoring"
            / "grafana"
            / "provisioning"
            / "dashboards"
            / "toss-trader.yml"
        ).read_text()

        self.assertIn("/etc/grafana/dashboards/toss-trader", provisioning)

    def test_grafana_datasource_uses_compose_prometheus(self) -> None:
        datasource = (
            ROOT
            / "monitoring"
            / "grafana"
            / "provisioning"
            / "datasources"
            / "toss-trader.yml"
        ).read_text()

        self.assertIn("uid: toss-prometheus", datasource)
        self.assertIn("url: http://prometheus:9090", datasource)

    def test_compose_binds_monitoring_to_tailscale(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()

        self.assertIn("100.74.208.69", compose)
        self.assertIn("prometheus:", compose)
        self.assertIn("alertmanager:", compose)
        self.assertIn("${PROMETHEUS_PORT:-19090}:9090", compose)
        self.assertIn("${ALERTMANAGER_PORT:-19093}:9093", compose)
        self.assertIn("TELEGRAM_BOT_TOKEN", compose)
        self.assertIn("TELEGRAM_CHAT_ID", compose)
        self.assertIn("TELEGRAM_TOPIC", compose)
        self.assertIn("context: ./monitoring/prometheus", compose)
        self.assertIn("context: ./monitoring/alertmanager", compose)
        self.assertNotIn("toss-trader-grafana", compose)
        self.assertNotIn("${GRAFANA_PORT:-13000}:3000", compose)
        self.assertNotIn("scrape-job.yml:/etc/prometheus", compose)

    def test_compose_automation_is_internal_paper_only(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()

        self.assertIn("automation:", compose)
        self.assertIn('command: ["serve-automation"]', compose)
        self.assertIn("HERMES_API_KEY", compose)
        self.assertIn("toss-trader-automation", compose)
        self.assertIn("openclaw-net", compose)
        self.assertIn("RISK_MANAGER_WEBHOOK_URL: http://n8n:5678/webhook/", compose)
        self.assertIn("N8N_RISK_MANAGER_TOKEN", compose)
        self.assertNotIn("/var/run/docker.sock", compose)

    def test_pit_collector_receives_shared_postgres_settings(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        pit_block = compose.split("  pit-collector:", 1)[1].split(
            "  paper-mcp:", 1
        )[0]

        self.assertIn("POSTGRES_HOST: ${POSTGRES_HOST:-}", pit_block)
        self.assertIn("POSTGRES_PORT: ${POSTGRES_PORT:-5431}", pit_block)
        self.assertIn("POSTGRES_USER: ${POSTGRES_USER:-}", pit_block)
        self.assertIn("POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-}", pit_block)
        self.assertIn("POSTGRES_DB: ${POSTGRES_DB:-}", pit_block)

    def test_compose_uses_isolated_zero_tool_hermes_sidecar(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        hermes_config = (
            ROOT / "automation" / "hermes-analysis" / "config.yaml"
        ).read_text()

        self.assertIn("hermes-analysis:", compose)
        self.assertIn("HERMES_API_BASE_URL: http://hermes-analysis:8642", compose)
        self.assertIn("API_SERVER_KEY: ${HERMES_API_KEY", compose)
        self.assertIn("hermes-analysis-data:/opt/data", compose)
        self.assertNotIn("8642:8642", compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertIn("api_server: []", hermes_config)
        self.assertIn("enabled: []", hermes_config)
        self.assertIn("mcp_servers: {}", hermes_config)
        self.assertIn("context:\n  engine: compressor", hermes_config)
        self.assertIn("memory_enabled: false", hermes_config)

    def test_n8n_workflow_runs_weekdays_after_market_close(self) -> None:
        workflow = json.loads(
            (ROOT / "automation" / "n8n" / "toss-trader-daily.json").read_text()
        )
        nodes = {node["name"]: node for node in workflow["nodes"]}

        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["settings"]["timezone"], "Asia/Seoul")
        self.assertIn("40 15 * * 1-5", json.dumps(nodes["평일 15:40 KST"]))
        encoded = json.dumps(workflow, ensure_ascii=False)
        self.assertIn("/workflow/paper-rule-1d", encoded)
        self.assertIn("/workflow/paper-hermes-1d", encoded)
        self.assertIn("/workflow/hermes-daily-result", encoded)
        self.assertIn("/workflow/report-daily", encoded)
        self.assertIn("http://hermes-analysis:8642/v1/chat/completions", encoded)
        self.assertIn("toss-trader-hermes-auth", encoded)
        self.assertIn("n8n-nodes-base.webhook", encoded)
        self.assertIn('"path": "toss-trader-daily-run"', encoded)
        self.assertIn('"authentication": "headerAuth"', encoded)
        self.assertIn('"responseMode": "onReceived"', encoded)
        self.assertIn("toss-trader-manual-trigger-auth", encoded)
        self._assert_scheduled_runs_use_toss_market_calendar(workflow)
        for branch in (
            "Rule 일봉 정상?",
            "Rule 일봉 체결 있음?",
            "Hermes 일봉 정상?",
            "Hermes 일봉 체결 있음?",
            "Hermes 마감 분석 정상?",
            "마감 Telegram 정상?",
        ):
            self.assertIn(branch, nodes)

    def test_n8n_workflow_runs_intraday_paper_every_five_minutes(self) -> None:
        workflow = json.loads(
            (
                ROOT / "automation" / "n8n" / "toss-trader-intraday-paper.json"
            ).read_text()
        )
        encoded = json.dumps(workflow, ensure_ascii=False)

        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["settings"]["timezone"], "Asia/Seoul")
        self.assertIn("*/5 9-14 * * 1-5", encoded)
        self.assertIn("0-20/5 15 * * 1-5", encoded)
        self.assertIn("/workflow/paper-rule-1m", encoded)
        self.assertIn("/workflow/paper-hermes-1m", encoded)
        self.assertIn("/workflow/report-paper", encoded)
        self.assertIn("비교 결과 병합", encoded)
        self._assert_scheduled_runs_use_toss_market_calendar(workflow)
        self.assertIn("시장 Snapshot + Rule 1분봉", encoded)
        self.assertIn("공유 Snapshot + Hermes 1분봉", encoded)
        for branch in (
            "Rule Cycle 정상?",
            "Rule 체결 있음?",
            "Hermes Cycle 정상?",
            "Hermes 체결 있음?",
            "특이사항 있음?",
        ):
            self.assertIn(branch, encoded)

    def test_intraday_http_json_bodies_use_explicit_fields_not_spread(self) -> None:
        workflow = json.loads(
            (
                ROOT / "automation" / "n8n" / "toss-trader-intraday-paper.json"
            ).read_text()
        )
        http_nodes = {
            node["name"]: node["parameters"]["jsonBody"]
            for node in workflow["nodes"]
            if node["type"] == "n8n-nodes-base.httpRequest"
        }
        for name, body in http_nodes.items():
            self.assertNotIn("...$json", body, name)
            self.assertNotIn("... $json", body, name)

        report = http_nodes["특이사항 Telegram"]
        self.assertIn("/workflow/report-paper", json.dumps(workflow, ensure_ascii=False))
        self.assertIn("$('시장 Snapshot + Rule 1분봉').first().json", report)
        self.assertIn("$('공유 Snapshot + Hermes 1분봉').first().json", report)
        self.assertIn('"rule"', report)
        self.assertIn('"hermes"', report)
        self.assertIn('"_workflow"', report)

        hermes = http_nodes["공유 Snapshot + Hermes 1분봉"]
        self.assertIn("$('시장 Snapshot + Rule 1분봉').first().json", hermes)
        self.assertIn('"rule"', hermes)

    def test_n8n_workflow_sends_weekday_market_discovery_report(self) -> None:
        workflow = json.loads(
            (ROOT / "automation" / "n8n" / "toss-trader-market-scan.json").read_text()
        )
        encoded = json.dumps(workflow, ensure_ascii=False)

        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["settings"]["timezone"], "Asia/Seoul")
        self.assertIn("30 8 * * 1-5", encoded)
        self.assertIn("/workflow/market-scan", encoded)
        self.assertIn("/workflow/hermes-market-result", encoded)
        self.assertIn("/workflow/report-market", encoded)
        self._assert_scheduled_runs_use_toss_market_calendar(workflow)
        self.assertIn("http://hermes-analysis:8642/v1/chat/completions", encoded)
        self.assertIn("toss-trader-hermes-auth", encoded)
        self.assertNotIn("HERMES_API_KEY", encoded)
        for branch in (
            "시장 스캔 정상?",
            "발굴 후보 있음?",
            "Hermes 의견 정상?",
            "Telegram 전송 정상?",
        ):
            self.assertIn(branch, encoded)

    def _assert_scheduled_runs_use_toss_market_calendar(
        self, workflow: dict
    ) -> None:
        nodes = {node["name"]: node for node in workflow["nodes"]}
        schedule_names = {
            node["name"]
            for node in workflow["nodes"]
            if node["type"] == "n8n-nodes-base.scheduleTrigger"
        }
        lookup = nodes["Toss 한국장 일정 확인"]
        gate = nodes["한국장 영업일?"]

        self.assertEqual(
            lookup["parameters"]["url"],
            "http://toss-trader-automation:8088/workflow/market-session",
        )
        self.assertTrue(lookup["retryOnFail"])
        self.assertEqual(lookup["maxTries"], 3)
        self.assertIn("isBusinessDay", json.dumps(gate, ensure_ascii=False))
        for schedule_name in schedule_names:
            target = workflow["connections"][schedule_name]["main"][0][0]["node"]
            self.assertEqual(target, "Toss 한국장 일정 확인")
        self.assertEqual(
            workflow["connections"]["Toss 한국장 일정 확인"]["main"][0][0][
                "node"
            ],
            "한국장 영업일?",
        )
    def test_n8n_http_stages_branch_on_application_failures(self) -> None:
        for filename in (
            "toss-trader-market-scan.json",
            "toss-trader-intraday-paper.json",
            "toss-trader-daily.json",
        ):
            workflow = json.loads((ROOT / "automation" / "n8n" / filename).read_text())
            http_nodes = [
                node
                for node in workflow["nodes"]
                if node["type"] == "n8n-nodes-base.httpRequest"
                and "/workflow/report-failure" not in node["parameters"]["url"]
                and "/workflow/market-session" not in node["parameters"]["url"]
            ]
            self.assertTrue(
                all(
                    node["parameters"]
                    .get("options", {})
                    .get("response", {})
                    .get("response", {})
                    .get("neverError")
                    for node in http_nodes
                )
            )
            self.assertTrue(
                any(node["type"] == "n8n-nodes-base.if" for node in workflow["nodes"])
            )

    def test_n8n_workflows_use_shared_failure_reporter(self) -> None:
        for filename in (
            "toss-trader-market-scan.json",
            "toss-trader-intraday-paper.json",
            "toss-trader-daily.json",
        ):
            workflow = json.loads((ROOT / "automation" / "n8n" / filename).read_text())
            self.assertEqual(
                workflow["settings"]["errorWorkflow"],
                "toss-trader-workflow-error",
            )
        error_workflow = json.loads(
            (ROOT / "automation" / "n8n" / "toss-trader-error.json").read_text()
        )
        encoded = json.dumps(error_workflow)
        self.assertIn("n8n-nodes-base.errorTrigger", encoded)
        self.assertIn("/workflow/report-failure", encoded)

    def test_n8n_risk_manager_uses_authenticated_webhook(self) -> None:
        workflow = json.loads(
            (ROOT / "automation" / "n8n" / "toss-trader-risk-manager.json").read_text()
        )
        encoded = json.dumps(workflow, ensure_ascii=False)

        self.assertFalse(workflow["active"])
        self.assertIn("n8n-nodes-base.webhook", encoded)
        self.assertIn('"authentication": "headerAuth"', encoded)
        self.assertIn("toss-trader-risk-manager-auth", encoded)
        self.assertNotIn("/workflow/risk-manager-evaluate", encoded)
        self.assertIn("parentExecutionId", encoded)

    def test_n8n_risk_manager_branches_and_evaluates_policy_in_workflow(self) -> None:
        workflow = json.loads(
            (ROOT / "automation" / "n8n" / "toss-trader-risk-manager.json").read_text()
        )
        encoded = json.dumps(workflow, ensure_ascii=False)
        node_names = {node["name"] for node in workflow["nodes"]}

        self.assertTrue(
            {
                "Trade 요청?",
                "Universe 요청?",
                "Trade 정책 계산",
                "Universe 정책 계산",
                "승인?",
                "승인 응답 구성",
                "거부 응답 구성",
            }.issubset(node_names)
        )
        self.assertGreaterEqual(encoded.count("n8n-nodes-base.if"), 3)
        self.assertIn("/workflow/risk-manager-audit", encoded)
        self.assertIn("input.limits", encoded)
        self.assertNotIn("dec('300000'", encoded)
        self.assertNotIn("dec('1000000'", encoded)
        self.assertNotIn("dec('-0.03'", encoded)
        self.assertIn(
            "side === 'BUY' && cmp(dailyReturnRate, dailyLossLimit) <= 0",
            encoded,
        )
        for violation in (
            "duplicate-signal",
            "universe-refresh-failed",
            "max-order-notional",
            "insufficient-paper-cash",
            "max-position-notional",
            "insufficient-position",
            "max-daily-buys",
            "max-open-positions",
            "daily-loss-limit",
            "api-error-kill-switch",
            "market-closed",
            "market-close-window",
            "unsupported-security-type",
            "not-common-share",
            "stock-not-active",
            "trading-suspended",
            "invalid-reference-price",
        ):
            self.assertIn(violation, encoded)

        code_by_name = {
            node["name"]: node.get("parameters", {}).get("jsCode", "")
            for node in workflow["nodes"]
        }
        trade_code = code_by_name["Trade 정책 계산"]
        universe_code = code_by_name["Universe 정책 계산"]
        for mutable_violation in (
            "max-order-notional",
            "insufficient-paper-cash",
            "daily-loss-limit",
            "api-error-kill-switch",
        ):
            self.assertIn(mutable_violation, trade_code)
            self.assertNotIn(mutable_violation, universe_code)

    def test_n8n_credentials_sync_from_infisical_without_literals(self) -> None:
        script = (
            ROOT / "automation" / "n8n" / "sync-infisical-credentials.sh"
        ).read_text()
        self.assertIn("infisical login", script)
        self.assertIn("--env=prod", script)
        self.assertIn("--path=/", script)
        self.assertIn("n8n import:credentials", script)
        self.assertIn('type: "httpHeaderAuth"', script)
        self.assertIn('type: "oAuth2Api"', script)
        self.assertIn("HERMES_API_KEY=$(get_secret HERMES_API_KEY)", script)
        self.assertIn("TOSS_CLIENT_SECRET=$(get_secret TOSS_CLIENT_SECRET)", script)
        self.assertIn(
            "N8N_RISK_MANAGER_TOKEN=$(get_secret N8N_RISK_MANAGER_TOKEN)", script
        )
        self.assertIn(
            "N8N_MANUAL_TRIGGER_TOKEN=$(get_secret N8N_MANUAL_TRIGGER_TOKEN)", script
        )
        self.assertIn('id: "toss-trader-manual-trigger-auth"', script)
        self.assertNotIn("risk-token-long-enough", script)
        self.assertNotIn("secretValue", script)

    def test_daily_webhook_runner_keeps_token_out_of_curl_arguments(self) -> None:
        script = (
            ROOT / "automation" / "n8n" / "run-daily-webhook.sh"
        ).read_text()

        self.assertIn("N8N_MANUAL_TRIGGER_TOKEN", script)
        self.assertIn("--header @-", script)
        self.assertIn("toss-trader-daily-run", script)
        self.assertNotIn(
            '--header "Authorization: Bearer $N8N_MANUAL_TRIGGER_TOKEN"', script
        )

    def test_monitoring_images_embed_remote_deployment_assets(self) -> None:
        prometheus_dockerfile = (
            ROOT / "monitoring" / "prometheus" / "Dockerfile"
        ).read_text()
        grafana_dockerfile = (
            ROOT / "monitoring" / "grafana" / "Dockerfile"
        ).read_text()

        self.assertIn(
            "COPY scrape-job.yml /etc/prometheus/prometheus.yml",
            prometheus_dockerfile,
        )
        self.assertIn(
            "COPY alerts.yml /etc/prometheus/alerts.yml", prometheus_dockerfile
        )
        self.assertIn("COPY provisioning /etc/grafana/provisioning", grafana_dockerfile)
        self.assertIn(
            "COPY dashboards /etc/grafana/dashboards/toss-trader",
            grafana_dockerfile,
        )

    def test_alertmanager_routes_firing_and_resolved_alerts_to_telegram(self) -> None:
        directory = ROOT / "monitoring" / "alertmanager"
        template = (directory / "config.template.yml").read_text()
        renderer = directory / "render-config.sh"
        dockerfile = (directory / "Dockerfile").read_text()

        self.assertIn("telegram_configs:", template)
        self.assertIn("send_resolved: true", template)
        self.assertIn("__TELEGRAM_BOT_TOKEN__", template)
        self.assertIn("__TELEGRAM_CHAT_ID__", template)
        self.assertIn("message_thread_id: __TELEGRAM_TOPIC__", template)
        self.assertIn("TossTraderMarketScan", template)
        self.assertIn("TossTraderPaperCycleNotice", template)
        self.assertIn("group_wait: 0s", template)
        self.assertIn("umask 077", renderer.read_text())
        self.assertIn("unset TELEGRAM_BOT_TOKEN", renderer.read_text())
        self.assertIn("TELEGRAM_TOPIC must be a positive integer", renderer.read_text())
        self.assertIn("COPY config.template.yml", dockerfile)
        self.assertIn("COPY --chmod=755 render-config.sh", dockerfile)
        self.assertIn('"--cluster.listen-address="', dockerfile)
        subprocess.run(["sh", "-n", renderer], check=True)


if __name__ == "__main__":
    unittest.main()
