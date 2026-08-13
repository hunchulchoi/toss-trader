import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MonitoringAssetsTest(unittest.TestCase):
    def test_grafana_dashboard_queries_exported_metrics(self) -> None:
        dashboard_path = (
            ROOT / "monitoring" / "grafana" / "dashboards" / "toss-trader.json"
        )
        dashboard = json.loads(dashboard_path.read_text())
        expressions = {
            target["expr"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if "expr" in target
        }
        sql_queries = {
            target["rawSql"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if "rawSql" in target
        }

        self.assertEqual(dashboard["uid"], "toss-trader")
        self.assertGreaterEqual(len(dashboard["panels"]), 8)
        self.assertIn("toss_trader_cycle_last_success", expressions)
        self.assertIn("toss_trader_cycle_last_daily_return_ratio", expressions)
        self.assertIn("toss_trader_paper_position_quantity", expressions)
        self.assertIn("toss_trader_paper_initial_cash_krw", expressions)
        self.assertIn("toss_trader_paper_available_cash_krw", expressions)
        self.assertIn("toss_trader_paper_deployed_cash_krw", expressions)

        datasources = {
            panel["datasource"]["uid"]
            for panel in dashboard["panels"]
            if "datasource" in panel
        }
        self.assertEqual(datasources, {"toss-prometheus", "toss-postgres"})
        self.assertTrue(
            any("paper_risk_decisions" in query for query in sql_queries)
        )
        self.assertTrue(
            any("automation_run_logs" in query for query in sql_queries)
        )
        self.assertTrue(any("paper_cycle_runs" in query for query in sql_queries))
        self.assertTrue(any("paper_fills" in query for query in sql_queries))
        self.assertTrue(
            any("dynamic_universe_runs" in query for query in sql_queries)
        )
        self.assertTrue(
            any("dynamic_universe_decisions" in query for query in sql_queries)
        )
        self.assertTrue(any("prompt_tokens" in query for query in sql_queries))
        titles = {panel["title"] for panel in dashboard["panels"]}
        self.assertIn("Paper Cycle Run Log", titles)
        cycle_panel = next(
            panel
            for panel in dashboard["panels"]
            if panel["title"] == "Paper Cycle Run Log"
        )
        self.assertEqual(cycle_panel["type"], "timeseries")
        self.assertEqual(cycle_panel["targets"][0]["format"], "time_series")
        self.assertIn('AS "Duration (ms)"', cycle_panel["targets"][0]["rawSql"])
        symbol_panel = next(
            panel
            for panel in dashboard["panels"]
            if panel["title"].startswith("Queried Symbols")
        )
        self.assertEqual(symbol_panel["type"], "timeseries")
        self.assertEqual(symbol_panel["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertIn("market_candles", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("interval = '1m'", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("market_symbols", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("display_name", symbol_panel["targets"][0]["rawSql"])
        self.assertNotIn("CASE symbol", symbol_panel["targets"][0]["rawSql"])
        self.assertIn("Recent Paper Fills", titles)
        self.assertIn("Dynamic Universe Risk Decisions", titles)
        self.assertIn("Hermes Automation Run Log", titles)
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
        self.assertNotIn("/var/run/docker.sock", compose)

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
        self.assertIn(
            "http://toss-trader-automation:8088/run-daily",
            json.dumps(nodes["Paper + Hermes + Telegram"]),
        )

    def test_n8n_workflow_runs_intraday_paper_every_five_minutes(self) -> None:
        workflow = json.loads(
            (
                ROOT
                / "automation"
                / "n8n"
                / "toss-trader-intraday-paper.json"
            ).read_text()
        )
        encoded = json.dumps(workflow, ensure_ascii=False)

        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["settings"]["timezone"], "Asia/Seoul")
        self.assertIn("*/5 9-14 * * 1-5", encoded)
        self.assertIn("0-20/5 15 * * 1-5", encoded)
        self.assertIn(
            "http://toss-trader-automation:8088/run-paper-cycle", encoded
        )

    def test_n8n_workflow_sends_weekday_market_discovery_report(self) -> None:
        workflow = json.loads(
            (ROOT / "automation" / "n8n" / "toss-trader-market-scan.json").read_text()
        )
        encoded = json.dumps(workflow, ensure_ascii=False)

        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["settings"]["timezone"], "Asia/Seoul")
        self.assertIn("30 8 * * 1-5", encoded)
        self.assertIn(
            "http://toss-trader-automation:8088/run-market-scan", encoded
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
        self.assertIn(
            "COPY provisioning /etc/grafana/provisioning", grafana_dockerfile
        )
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
