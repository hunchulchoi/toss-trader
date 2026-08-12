import json
import unittest
from datetime import UTC, datetime

from toss_trader.automation import (
    AutomationBusy,
    DailyAutomation,
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
        self.assertEqual(len(analyzed), 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
