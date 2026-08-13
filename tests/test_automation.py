import json
import subprocess
import unittest
from datetime import UTC, datetime
from typing import Self
from unittest.mock import patch

from toss_trader.automation import (
    AlertmanagerReporter,
    AutomationBusy,
    AutomationRunLog,
    DailyAutomation,
    HermesAnalysis,
    HermesAnalyzer,
    IntradayPaperAutomation,
    MarketScanAutomation,
    PaperCycleProcess,
    PaperPortfolioProcess,
    WorkflowTaskService,
    automation_response,
    paper_cycle_notice,
)


class WorkflowTaskServiceTest(unittest.TestCase):
    def test_completes_direct_market_hermes_response_and_audits_tokens(self) -> None:
        audits: list[AutomationRunLog] = []

        class NeverCalledAnalyzer:
            def analyze(self, _: dict[str, object]) -> HermesAnalysis:
                raise AssertionError("Python must not call Hermes")

        class StubReporter:
            def report(self, _: dict[str, object]) -> dict[str, object]:
                return {"accepted": True}

        service = WorkflowTaskService(
            paper=None,  # type: ignore[arg-type]
            market_scan=None,  # type: ignore[arg-type]
            market_analyzer=NeverCalledAnalyzer(),  # type: ignore[arg-type]
            daily_analyzer=NeverCalledAnalyzer(),  # type: ignore[arg-type]
            market_reporter=StubReporter(),  # type: ignore[arg-type]
            paper_reporter=StubReporter(),  # type: ignore[arg-type]
            daily_reporter=StubReporter(),  # type: ignore[arg-type]
            failure_reporter=StubReporter(),  # type: ignore[arg-type]
            audit=lambda run: audits.append(run) or "audit-1",
            clock=lambda: datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
        )
        scan = {
            "exitCode": 0,
            "scan": {"markets": [], "candidates": [], "errors": []},
        }

        result = service.run(
            "/workflow/hermes-market-result",
            {
                "scan": scan,
                "hermesResponse": {
                    "choices": [{"message": {"content": "시장 의견"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                },
            },
        )

        self.assertEqual(result["opinion"], "시장 의견")
        self.assertEqual(result["hermesUsage"]["totalTokens"], 120)
        self.assertEqual(audits[0].run_type, "market_scan")
        self.assertEqual(audits[0].total_tokens, 120)
        self.assertEqual(audits[1].run_type, "n8n_flow")
        self.assertEqual(audits[1].stage, "hermes-market-result")
        self.assertEqual(audits[1].total_tokens, 120)

    def test_audits_n8n_stage_context_without_request_payload(self) -> None:
        audits: list[AutomationRunLog] = []

        class StubProcess:
            def run(self) -> dict[str, object]:
                return {
                    "exitCode": 0,
                    "scan": {"markets": [], "candidates": [], "errors": []},
                }

        class StubReporter:
            def report(self, _: dict[str, object]) -> dict[str, object]:
                return {"accepted": True}

        service = WorkflowTaskService(
            paper=None,  # type: ignore[arg-type]
            market_scan=StubProcess(),  # type: ignore[arg-type]
            market_analyzer=None,  # type: ignore[arg-type]
            daily_analyzer=None,  # type: ignore[arg-type]
            market_reporter=StubReporter(),  # type: ignore[arg-type]
            paper_reporter=StubReporter(),  # type: ignore[arg-type]
            daily_reporter=StubReporter(),  # type: ignore[arg-type]
            failure_reporter=StubReporter(),  # type: ignore[arg-type]
            audit=lambda run: audits.append(run) or "flow-audit-1",
            clock=lambda: datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
        )

        service.run(
            "/workflow/market-scan",
            {
                "_workflow": {
                    "workflowId": "toss-trader-market-scan",
                    "executionId": "321",
                    "trigger": "production",
                    "stage": "market-scan",
                },
                "authorization": "must-not-be-audited",
            },
        )

        self.assertEqual(audits[0].run_type, "n8n_flow")
        self.assertEqual(audits[0].status, "succeeded")
        self.assertEqual(audits[0].details["executionId"], "321")  # type: ignore[index]
        self.assertNotIn("authorization", json.dumps(audits[0].details))

    def test_failure_report_is_compact_and_keeps_error_details_in_audit(self) -> None:
        audits: list[AutomationRunLog] = []
        reports: list[dict[str, object]] = []

        class StubReporter:
            def report(self, value: dict[str, object]) -> dict[str, object]:
                reports.append(value)
                return {"accepted": True}

        service = WorkflowTaskService(
            paper=None,  # type: ignore[arg-type]
            market_scan=None,  # type: ignore[arg-type]
            market_analyzer=None,  # type: ignore[arg-type]
            daily_analyzer=None,  # type: ignore[arg-type]
            market_reporter=StubReporter(),
            paper_reporter=StubReporter(),
            daily_reporter=StubReporter(),
            failure_reporter=StubReporter(),
            audit=lambda run: audits.append(run) or "audit-1",
            clock=lambda: datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
        )

        service.run(
            "/workflow/report-failure",
            {
                "_workflow": {
                    "workflowId": "toss-trader-daily",
                    "executionId": "162",
                    "stage": "rule-cycle",
                    "portfolioId": "rule",
                    "interval": "1d",
                    "status": "failed",
                },
                "response": {
                    "exitCode": 3,
                    "cycle": {
                        "summary": {"symbols": 17, "failed": 1, "fills": 0},
                        "items": [
                            {"symbol": "487400", "error": "daily candles unavailable"}
                        ],
                    },
                },
            },
        )

        message = str(reports[0]["analysis"])
        self.assertIn("workflow: toss-trader-daily / execution: 162", message)
        self.assertIn("단계: rule-cycle (rule · 1d)", message)
        self.assertIn("종목 처리 실패: 1/17", message)
        self.assertIn("487400 오류: daily candles unavailable", message)
        self.assertNotIn('"sharedSnapshot"', message)
        self.assertEqual(reports[0]["severity"], "warning")
        self.assertEqual(audits[0].details["failure"]["errors"][0]["symbol"], "487400")  # type: ignore[index]

    def test_rejects_direct_hermes_response_without_content(self) -> None:
        class Stub:
            def report(self, _: dict[str, object]) -> dict[str, object]:
                return {}

        service = WorkflowTaskService(
            paper=None,  # type: ignore[arg-type]
            market_scan=None,  # type: ignore[arg-type]
            market_analyzer=None,  # type: ignore[arg-type]
            daily_analyzer=None,  # type: ignore[arg-type]
            market_reporter=Stub(),  # type: ignore[arg-type]
            paper_reporter=Stub(),  # type: ignore[arg-type]
            daily_reporter=Stub(),  # type: ignore[arg-type]
            failure_reporter=Stub(),  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(RuntimeError, "missing content"):
            service.run(
                "/workflow/hermes-market-result",
                {
                    "scan": {"exitCode": 0, "scan": {}},
                    "hermesResponse": {"choices": []},
                },
            )

    def test_evaluates_risk_manager_workflow_payload(self) -> None:
        class Stub:
            def report(self, _: dict[str, object]) -> dict[str, object]:
                return {}

        service = WorkflowTaskService(
            paper=None,  # type: ignore[arg-type]
            market_scan=None,  # type: ignore[arg-type]
            market_analyzer=None,  # type: ignore[arg-type]
            daily_analyzer=None,  # type: ignore[arg-type]
            market_reporter=Stub(),  # type: ignore[arg-type]
            paper_reporter=Stub(),  # type: ignore[arg-type]
            daily_reporter=Stub(),  # type: ignore[arg-type]
            failure_reporter=Stub(),  # type: ignore[arg-type]
        )

        result = service.run(
            "/workflow/risk-manager-evaluate",
            {
                "kind": "trade",
                "signal": {
                    "signalId": "signal-1",
                    "symbol": "005930",
                    "side": "SELL",
                    "referencePrice": "70000",
                    "quantity": "2",
                    "reason": "MA20 crossed below MA60",
                },
                "context": {
                    "now": "2026-08-13T06:00:00Z",
                    "marketCloseAt": "2026-08-13T06:30:00Z",
                    "marketIsBusinessDay": True,
                    "positionNotional": "70000",
                    "positionQuantity": "1",
                    "availableCash": "900000",
                    "dailyBuyCount": 0,
                    "openPositionCount": 1,
                    "dailyReturnRate": "0",
                    "consecutiveApiErrors": 0,
                    "seenSignalIds": [],
                    "newBuysAllowed": True,
                    "advisorStatus": None,
                    "advisorRationale": None,
                },
            },
        )

        self.assertFalse(result["approved"])
        self.assertEqual(result["violations"], ["insufficient-position"])


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

    def test_records_success_with_hermes_token_usage(self) -> None:
        audits: list[AutomationRunLog] = []
        service = DailyAutomation(
            run_cycle=lambda: {
                "exitCode": 0,
                "cycle": {"summary": {"symbols": 1, "fills": 0}},
            },
            analyze=lambda _: HermesAnalysis(
                content="정상",
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
            report=lambda _: {"accepted": True},
            audit=lambda run: audits.append(run) or "audit-daily-1",
            clock=lambda: datetime(2026, 8, 12, 6, 40, tzinfo=UTC),
        )

        result = service.run()

        self.assertEqual(result["auditRunId"], "audit-daily-1")
        self.assertEqual(result["hermesUsage"]["totalTokens"], 120)
        self.assertEqual(audits[0].status, "succeeded")
        self.assertEqual(audits[0].total_tokens, 120)

    def test_reports_failure_when_hermes_fails(self) -> None:
        reports: list[dict[str, object]] = []
        audits: list[AutomationRunLog] = []

        def fail(_: dict[str, object]) -> str:
            raise RuntimeError("Hermes unavailable")

        service = DailyAutomation(
            run_cycle=lambda: {"exitCode": 0, "cycle": {}},
            analyze=fail,
            report=lambda value: reports.append(value) or {"accepted": True},
            audit=lambda run: audits.append(run) or "audit-failed-1",
        )

        with self.assertRaisesRegex(RuntimeError, "hermes stage failed"):
            service.run()

        self.assertEqual(reports[0]["stage"], "hermes")
        self.assertFalse(reports[0]["ok"])
        self.assertEqual(audits[0].status, "failed")
        self.assertEqual(audits[0].stage, "hermes")

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


class IntradayPaperAutomationTest(unittest.TestCase):
    def test_runs_one_minute_cycle_without_hermes_and_skips_normal_notice(self) -> None:
        notices: list[dict[str, object]] = []
        service = IntradayPaperAutomation(
            run_cycle=lambda: {
                "exitCode": 0,
                "cycle": {
                    "interval": "1m",
                    "summary": {"symbols": 1, "signals": 0, "fills": 0, "failed": 0},
                    "items": [{"symbol": "005930"}],
                },
            },
            report_notice=lambda value: notices.append(value) or {"accepted": True},
            clock=lambda: datetime(2026, 8, 13, 0, 5, tzinfo=UTC),
        )

        result = service.run()

        self.assertTrue(result["ok"])
        self.assertEqual(result["cycle"]["cycle"]["interval"], "1m")
        self.assertEqual(result["notices"], [])
        self.assertEqual(notices, [])
        self.assertNotIn("analysis", result)
        self.assertNotIn("hermesUsage", result)

    def test_reports_paper_fill_notice(self) -> None:
        notices: list[dict[str, object]] = []
        service = IntradayPaperAutomation(
            run_cycle=lambda: {
                "exitCode": 0,
                "cycle": {
                    "summary": {"symbols": 1, "signals": 1, "fills": 1, "failed": 0},
                    "items": [
                        {
                            "symbol": "005930",
                            "fill": {
                                "side": "BUY",
                                "quantity": "1000",
                                "price": "71000",
                            },
                        }
                    ],
                },
            },
            report_notice=lambda value: notices.append(value) or {"accepted": True},
        )

        result = service.run()

        self.assertEqual(result["noticeReported"], {"accepted": True})
        self.assertIn(
            "paper 체결: 005930 BUY 1,000 @ 71,000",
            notices[0]["analysis"],
        )

    def test_process_forces_one_minute_interval_and_disables_trading(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"interval":"1m","summary":{}}',
            stderr="",
        )
        with patch("subprocess.run", return_value=completed) as run:
            result = PaperCycleProcess(interval="1m").run()

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command[-2:], ["--interval", "1m"])
        self.assertEqual(environment["TRADING_ENABLED"], "false")
        self.assertEqual(result["cycle"]["interval"], "1m")

    def test_hermes_task_reuses_rule_market_snapshot(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"summary":{}}', stderr=""
        )
        rule = {
            "cycle": {
                "universe": {
                    "runId": "universe-1",
                    "symbols": ["005930", "000660"],
                    "entrySymbols": ["005930"],
                },
                "sharedSnapshot": {
                    "version": 1,
                    "symbols": ["005930", "000660"],
                    "interval": "1m",
                    "evaluatedAt": "2026-08-13T05:00:00+00:00",
                    "collections": [None, None],
                    "signals": [None, None],
                    "errors": [None, None],
                    "apiFailed": False,
                    "newBuysAllowed": True,
                },
            }
        }

        with patch("subprocess.run", return_value=completed) as run:
            result = PaperPortfolioProcess().run(
                portfolio_id="hermes", interval="1m", rule_cycle=rule
            )

        command = run.call_args.args[0]
        self.assertIn("--hermes-advisor", command)
        self.assertIn("--snapshot-stdin", command)
        self.assertNotIn("--symbols", command)
        submitted = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(submitted["symbols"], ["005930", "000660"])
        self.assertEqual(submitted["interval"], "1m")
        self.assertEqual(result["exitCode"], 0)


class DailyAutomationNoticeTest(unittest.TestCase):
    def test_reports_noteworthy_cycle_before_hermes(self) -> None:
        calls: list[tuple[str, object]] = []
        cycle = {
            "exitCode": 0,
            "cycle": {
                "summary": {"symbols": 1, "signals": 1, "fills": 1, "failed": 0},
                "items": [
                    {
                        "symbol": "005930",
                        "fill": {
                            "side": "BUY",
                            "quantity": "1",
                            "price": "71000",
                        },
                    }
                ],
            },
        }

        service = DailyAutomation(
            run_cycle=lambda: calls.append(("cycle", None)) or cycle,
            analyze=lambda value: calls.append(("hermes", value)) or "체결 1건",
            report=lambda value: calls.append(("report", value)) or {"accepted": True},
            report_notice=lambda value: (
                calls.append(("notice", value)) or {"accepted": True}
            ),
        )

        result = service.run()

        self.assertEqual(
            [name for name, _ in calls], ["cycle", "notice", "hermes", "report"]
        )
        notice = calls[1][1]
        self.assertEqual(notice["severity"], "info")
        self.assertIn("paper 체결: 005930 BUY 1 @ 71,000", notice["analysis"])
        self.assertEqual(result["noticeReported"], {"accepted": True})

    def test_skips_notice_for_normal_cycle_without_events(self) -> None:
        notices: list[dict[str, object]] = []
        service = DailyAutomation(
            run_cycle=lambda: {
                "exitCode": 0,
                "cycle": {
                    "summary": {
                        "symbols": 1,
                        "signals": 0,
                        "fills": 0,
                        "failed": 0,
                    },
                    "items": [{"symbol": "005930"}],
                },
            },
            analyze=lambda _: "정상",
            report=lambda _: {"accepted": True},
            report_notice=lambda value: notices.append(value) or {"accepted": True},
        )

        result = service.run()

        self.assertEqual(notices, [])
        self.assertEqual(result["notices"], [])
        self.assertNotIn("noticeReported", result)

    def test_notice_failure_does_not_block_daily_analysis_and_report(self) -> None:
        calls: list[str] = []

        def fail_notice(_: dict[str, object]) -> dict[str, object]:
            calls.append("notice")
            raise RuntimeError("Alertmanager notice failed")

        service = DailyAutomation(
            run_cycle=lambda: {
                "exitCode": 0,
                "cycle": {
                    "summary": {
                        "symbols": 1,
                        "signals": 1,
                        "fills": 1,
                        "failed": 0,
                    },
                    "items": [
                        {
                            "symbol": "005930",
                            "fill": {
                                "side": "BUY",
                                "quantity": "1",
                                "price": "71000",
                            },
                        }
                    ],
                },
            },
            analyze=lambda _: calls.append("hermes") or "일일 분석",
            report=lambda _: calls.append("report") or {"accepted": True},
            report_notice=fail_notice,
        )

        result = service.run()

        self.assertEqual(calls, ["notice", "hermes", "report"])
        self.assertEqual(result["analysis"], "일일 분석")
        self.assertEqual(result["reported"], {"accepted": True})
        self.assertEqual(result["noticeReportError"], "Alertmanager notice failed")


class PaperCycleNoticeTest(unittest.TestCase):
    def test_detects_failures_risk_rejection_api_streak_and_loss_limit(self) -> None:
        notice = paper_cycle_notice(
            {
                "exitCode": 3,
                "cycle": {
                    "dailyReturnRate": "-0.031",
                    "consecutiveApiErrors": 5,
                    "summary": {
                        "symbols": 2,
                        "signals": 1,
                        "fills": 0,
                        "failed": 1,
                    },
                    "items": [
                        {"symbol": "005930", "error": "Toss API timeout"},
                        {
                            "symbol": "AAPL",
                            "decision": {
                                "approved": False,
                                "violations": ["daily-loss-limit"],
                            },
                        },
                    ],
                },
            }
        )

        self.assertIsNotNone(notice)
        self.assertEqual(notice.severity, "critical")
        self.assertIn("cycle 종료 코드: 3", notice.lines)
        self.assertIn("종목 처리 실패: 1/2", notice.lines)
        self.assertIn("Toss API 오류 연속: 5회", notice.lines)
        self.assertIn("일일 손실 한도 도달: -3.10%", notice.lines)
        self.assertIn("005930 오류: Toss API timeout", notice.lines)
        self.assertIn("AAPL RiskManager 거부: daily-loss-limit", notice.lines)

    def test_ignores_duplicate_signal_rejection(self) -> None:
        notice = paper_cycle_notice(
            {
                "exitCode": 0,
                "cycle": {
                    "summary": {"symbols": 1, "signals": 1, "fills": 0, "failed": 0},
                    "items": [
                        {
                            "symbol": "005930",
                            "decision": {
                                "approved": False,
                                "violations": ["duplicate-signal"],
                            },
                        }
                    ],
                },
            }
        )

        self.assertIsNone(notice)


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
                    {
                        "choices": [{"message": {"content": "시장 의견입니다."}}],
                        "usage": {
                            "prompt_tokens": 436,
                            "completion_tokens": 6,
                            "total_tokens": 442,
                        },
                    }
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
        self.assertEqual(result.content, "시장 의견입니다.")
        self.assertEqual(result.prompt_tokens, 436)
        self.assertEqual(result.completion_tokens, 6)
        self.assertEqual(result.total_tokens, 442)
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

    def test_cycle_failure_preserves_generated_analysis(self) -> None:
        transport: list[dict[str, object]] = []

        class Response:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b""

        def open_request(request, timeout):
            del timeout
            transport.extend(json.loads(request.data))
            return Response()

        reporter = AlertmanagerReporter(url="http://alertmanager")
        with patch("urllib.request.urlopen", side_effect=open_request):
            reporter.report(
                {
                    "ok": False,
                    "analysis": "종목 처리 실패: 15/15\n487400 오류: 일봉 부족",
                }
            )

        description = transport[0]["annotations"]["description"]
        self.assertIn("종목 처리 실패: 15/15", description)
        self.assertIn("487400 오류: 일봉 부족", description)
        self.assertNotIn("unknown: failed", description)

    def test_uses_explicit_notice_severity(self) -> None:
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

        reporter = AlertmanagerReporter(alert_name="TossTraderPaperCycleNotice")
        with patch("urllib.request.urlopen", side_effect=send):
            result = reporter.report(
                {
                    "ok": True,
                    "cycle": {"exitCode": 0},
                    "severity": "critical",
                    "analysis": "일일 손실 한도 도달",
                }
            )

        self.assertEqual(result["severity"], "critical")
        self.assertEqual(captured[0][0]["labels"]["severity"], "critical")


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
        daily = DailyAutomation(
            run_cycle=dict, analyze=lambda _: "ok", report=lambda _: {}
        )
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

    def test_intraday_route_runs_paper_only_service(self) -> None:
        daily = DailyAutomation(
            run_cycle=dict, analyze=lambda _: "ok", report=lambda _: {}
        )
        intraday = IntradayPaperAutomation(
            run_cycle=lambda: {
                "exitCode": 0,
                "cycle": {"interval": "1m", "summary": {}},
            },
            report_notice=lambda _: {"accepted": True},
        )

        status, payload = automation_response(
            "POST", "/run-paper-cycle", daily, intraday_service=intraday
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cycle"]["cycle"]["interval"], "1m")

    def test_routes_n8n_workflow_task_with_json_payload(self) -> None:
        daily = DailyAutomation(
            run_cycle=dict, analyze=lambda _: "ok", report=lambda _: {}
        )
        calls: list[tuple[str, dict[str, object]]] = []

        class WorkflowStub:
            def run(self, path: str, payload: dict[str, object]) -> dict[str, object]:
                calls.append((path, payload))
                return {"exitCode": 0, "cycle": {"portfolioId": "hermes"}}

        status, payload = automation_response(
            "POST",
            "/workflow/paper-hermes-1m",
            daily,
            workflow_service=WorkflowStub(),  # type: ignore[arg-type]
            payload={"rule": {"exitCode": 0}},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["cycle"]["portfolioId"], "hermes")
        self.assertEqual(
            calls,
            [("/workflow/paper-hermes-1m", {"rule": {"exitCode": 0}})],
        )


if __name__ == "__main__":
    unittest.main()
