import json
import unittest
from datetime import UTC, datetime
from typing import Self
from unittest.mock import patch

from toss_trader.automation import (
    AlertmanagerReporter,
    AutomationBusy,
    DailyAutomation,
    HermesAnalyzer,
    MarketScanAutomation,
    automation_response,
)


class DailyAutomationTest(unittest.TestCase):
    def test_runs_cycle_then_hermes_then_report(self) -> None:
        calls: list[tuple[str, object]] = []
        cycle = {"exitCode": 0, "cycle": {"summary": {"failed": 0}}}

        service = DailyAutomation(
            run_cycle=lambda: calls.append(("cycle", None)) or cycle,
            analyze=lambda value: calls.append(("hermes", value)) or "정상 완료",
            report=lambda value: calls.append(("report", value)) or {"accepted": True},
            clock=lambda: datetime(2026, 8, 12, 6, 40, tzinfo=UTC),
        )

        result = service.run()

        self.assertEqual([name for name, _ in calls], ["cycle", "hermes", "report"])
        self.assertEqual(result["analysis"], "정상 완료")
        self.assertEqual(result["reported"], {"accepted": True})
        self.assertEqual(result["finishedAt"], "2026-08-12T06:40:00+00:00")

    def test_reports_failure_when_hermes_fails(self) -> None:
        reports: list[dict[str, object]] = []

        def fail(_: dict[str, object]) -> str:
            raise RuntimeError("Hermes unavailable")

        service = DailyAutomation(
            run_cycle=lambda: {"exitCode": 0, "cycle": {}},
            analyze=fail,
            report=lambda value: reports.append(value) or {"accepted": True},
        )

        with self.assertRaisesRegex(RuntimeError, "hermes stage failed"):
            service.run()

        self.assertEqual(reports[0]["stage"], "hermes")
        self.assertFalse(reports[0]["ok"])

    def test_rejects_concurrent_run(self) -> None:
        service = DailyAutomation(
            run_cycle=dict,
            analyze=lambda _: "ok",
            report=lambda _: {},
        )
        self.assertTrue(service._lock.acquire(blocking=False))
        try:
            with self.assertRaises(AutomationBusy):
                service.run()
        finally:
            service._lock.release()


class MarketScanAutomationTest(unittest.TestCase):
    def test_sends_only_market_scan_json_to_hermes(self) -> None:
        analyzed: list[dict[str, object]] = []
        scan = {
            "markets": [{"symbol": "069500", "regime": "NEUTRAL"}],
            "candidates": [{"symbol": "068270", "score": "18.0851"}],
            "errors": {},
        }
        service = MarketScanAutomation(
            run_scan=lambda: {"exitCode": 0, "scan": scan},
            analyze=lambda value: analyzed.append(value) or "LLM 시장 의견입니다.",
            report=lambda _: {"accepted": True},
        )

        result = service.run()

        self.assertTrue(result["ok"])
        self.assertEqual(analyzed, [scan])

    def test_reports_hermes_failure_without_fallback_opinion(self) -> None:
        reports: list[dict[str, object]] = []

        def fail(_: dict[str, object]) -> str:
            raise RuntimeError("Hermes API request failed")

        service = MarketScanAutomation(
            run_scan=lambda: {
                "exitCode": 0,
                "scan": {"markets": [], "candidates": [], "errors": {}},
            },
            analyze=fail,
            report=lambda value: reports.append(value) or {"accepted": True},
        )

        with self.assertRaisesRegex(RuntimeError, "hermes stage failed"):
            service.run()

        self.assertEqual(reports[0]["stage"], "hermes")
        self.assertNotIn("opinion", reports[0])
        self.assertNotIn("analysis", reports[0])


class HermesAnalyzerTest(unittest.TestCase):
    def test_posts_bearer_authenticated_json_without_tools(self) -> None:
        class Response:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": "시장 의견입니다."}}]}
                ).encode()

        scan = {"markets": [], "candidates": [], "errors": {}}
        analyzer = HermesAnalyzer(
            api_key="a" * 32,
            base_url="http://hermes-analysis:8642",
            system_prompt="시장 분석",
        )

        with patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            result = analyzer.analyze(scan)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(result, "시장 의견입니다.")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {'a' * 32}")
        self.assertEqual(json.loads(body["messages"][1]["content"]), scan)
        self.assertNotIn("tools", body)


class AlertmanagerReporterTest(unittest.TestCase):
    def test_hermes_failure_message_is_explicit(self) -> None:
        captured: list[list[dict[str, object]]] = []

        class Response:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b""

        def send(request: object, **_: object) -> Response:
            captured.append(json.loads(request.data))
            return Response()

        reporter = AlertmanagerReporter(alert_name="TossTraderMarketScan")
        with patch("urllib.request.urlopen", side_effect=send):
            reporter.report(
                {
                    "ok": False,
                    "stage": "hermes",
                    "error": "Hermes API request failed",
                }
            )

        description = captured[0][0]["annotations"]["description"]
        self.assertIn("Hermes 분석 실패", description)
        self.assertIn("Hermes API request failed", description)


class AutomationHttpTest(unittest.TestCase):
    def test_health_and_daily_run_routes(self) -> None:
        service = DailyAutomation(
            run_cycle=lambda: {"exitCode": 0},
            analyze=lambda _: "ok",
            report=lambda _: {"accepted": True},
        )

        health = automation_response("GET", "/healthz", service)
        run = automation_response("POST", "/run-daily", service)

        self.assertEqual(health, (200, {"status": "ok"}))
        self.assertEqual(run[0], 200)
        self.assertTrue(run[1]["ok"])

    def test_rejects_unknown_or_wrong_method(self) -> None:
        service = DailyAutomation(
            run_cycle=dict,
            analyze=lambda _: "ok",
            report=lambda _: {},
        )

        self.assertEqual(automation_response("GET", "/run-daily", service)[0], 405)
        self.assertEqual(automation_response("POST", "/unknown", service)[0], 404)

    def test_serializes_response_without_secrets(self) -> None:
        service = DailyAutomation(
            run_cycle=lambda: {"exitCode": 0},
            analyze=lambda _: "ok",
            report=lambda _: {"accepted": True},
        )

        _, payload = automation_response("POST", "/run-daily", service)

        encoded = json.dumps(payload)
        self.assertNotIn("API_KEY", encoded)
        self.assertNotIn("TOKEN", encoded)

    def test_market_scan_route_uses_llm_opinion(self) -> None:
        daily = DailyAutomation(run_cycle=dict, analyze=lambda _: "ok", report=lambda _: {})
        calls: list[dict[str, object]] = []
        analyzed: list[dict[str, object]] = []
        market = MarketScanAutomation(
            run_scan=lambda: {"exitCode": 0, "scan": {"markets": [], "candidates": []}},
            analyze=lambda value: analyzed.append(value) or "LLM 시장 의견",
            report=lambda value: calls.append(value) or {"accepted": True},
        )

        status, payload = automation_response(
            "POST", "/run-market-scan", daily, market_service=market
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["opinion"], "LLM 시장 의견")
        self.assertEqual(payload["reported"], {"accepted": True})
        self.assertEqual(analyzed, [{"markets": [], "candidates": []}])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
