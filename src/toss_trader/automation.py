from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .paper import open_paper_ledger
from .screening import format_market_scan_report

logger = logging.getLogger(__name__)


class AutomationBusy(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaperCycleNotice:
    severity: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HermesAnalysis:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AutomationRunLog:
    run_type: str
    status: str
    stage: str
    started_at: datetime
    finished_at: datetime
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error: str | None = None
    details: dict[str, object] | None = None


def paper_cycle_notice(job: dict[str, Any]) -> PaperCycleNotice | None:
    lines: list[str] = []
    severity = "info"
    exit_code = job.get("exitCode")
    if exit_code != 0:
        lines.append(f"cycle 종료 코드: {exit_code}")
        severity = "warning" if exit_code == 3 else "critical"

    cycle = job.get("cycle")
    if not isinstance(cycle, dict):
        lines.append("cycle 결과 JSON 누락")
        return PaperCycleNotice(severity="critical", lines=tuple(lines))
    if cycle.get("ok") is False:
        lines.append(f"cycle 실행 실패: {cycle.get('error', '원인 미상')}")
        severity = "critical"

    summary = cycle.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    failed = _non_negative_int(summary.get("failed"))
    symbols = _non_negative_int(summary.get("symbols"))
    if failed:
        lines.append(f"종목 처리 실패: {failed}/{symbols or '?'}")
        severity = _higher_severity(severity, "warning")

    api_errors = _non_negative_int(cycle.get("consecutiveApiErrors"))
    if api_errors:
        lines.append(f"Toss API 오류 연속: {api_errors}회")
        target = "critical" if api_errors >= 5 else "warning"
        severity = _higher_severity(severity, target)

    daily_return = _decimal(cycle.get("dailyReturnRate"))
    if daily_return is not None and daily_return <= Decimal("-0.03"):
        lines.append(f"일일 손실 한도 도달: {daily_return:.2%}")
        severity = "critical"

    items = cycle.get("items")
    if isinstance(items, list):
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            symbol = str(raw_item.get("symbol") or "unknown")
            error = raw_item.get("error")
            if error:
                lines.append(f"{symbol} 오류: {str(error)[:500]}")
                severity = _higher_severity(severity, "warning")
            fill = raw_item.get("fill")
            if isinstance(fill, dict):
                side = str(fill.get("side") or "?")
                quantity = str(fill.get("quantity") or "?")
                price = str(fill.get("price") or "?")
                lines.append(f"paper 체결: {symbol} {side} {quantity} @ {price}")
            decision = raw_item.get("decision")
            if not isinstance(decision, dict) or decision.get("approved") is not False:
                continue
            violations = decision.get("violations")
            violations = violations if isinstance(violations, list) else []
            noteworthy = [
                str(violation)
                for violation in violations
                if violation != "duplicate-signal"
            ]
            if noteworthy:
                lines.append(f"{symbol} RiskManager 거부: {', '.join(noteworthy)}")
                target = (
                    "critical"
                    if {"daily-loss-limit", "api-error-kill-switch"}
                    & set(noteworthy)
                    else "warning"
                )
                severity = _higher_severity(severity, target)

    if not lines:
        return None
    return PaperCycleNotice(severity=severity, lines=tuple(lines))


class DailyAutomation:
    def __init__(
        self,
        *,
        run_cycle: Callable[[], dict[str, Any]],
        analyze: Callable[[dict[str, Any]], str | HermesAnalysis],
        report: Callable[[dict[str, Any]], dict[str, Any]],
        report_notice: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        audit: Callable[[AutomationRunLog], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._run_cycle = run_cycle
        self._analyze = analyze
        self._report = report
        self._report_notice = report_notice
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def run(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise AutomationBusy("daily automation is already running")
        started_at = self._clock()
        usage = HermesAnalysis(content="")
        stage = "cycle"
        try:
            cycle = self._run_cycle()
            notice = paper_cycle_notice(cycle)
            notice_reported: dict[str, Any] | None = None
            notice_report_error: str | None = None
            if notice is not None and self._report_notice is not None:
                try:
                    notice_reported = self._report_notice(
                        {
                            "ok": True,
                            "cycle": cycle,
                            "analysis": "\n".join(notice.lines),
                            "severity": notice.severity,
                            "finishedAt": self._clock().isoformat(),
                        }
                    )
                except Exception as error:  # noqa: BLE001
                    notice_report_error = _safe_error(error)
                    logger.warning("paper cycle notice could not be sent")
            stage = "hermes"
            usage = _hermes_analysis(self._analyze(cycle))
            stage = "report"
            result = {
                "ok": True,
                "cycle": cycle,
                "analysis": usage.content,
                "hermesUsage": _hermes_usage(usage),
                "notices": list(notice.lines) if notice is not None else [],
                "finishedAt": self._clock().isoformat(),
            }
            if notice_reported is not None:
                result["noticeReported"] = notice_reported
            if notice_report_error is not None:
                result["noticeReportError"] = notice_report_error
            reported = self._report(result)
            result = {**result, "reported": reported}
            stage = "audit"
            audit_run_id = self._record_audit(
                status="succeeded",
                stage="completed",
                started_at=started_at,
                finished_at=self._clock(),
                usage=usage,
                details=_daily_run_details(cycle),
            )
            if audit_run_id is not None:
                result["auditRunId"] = audit_run_id
            return result
        except AutomationBusy:
            raise
        except Exception as error:
            failure = {
                "ok": False,
                "stage": stage,
                "error": _safe_error(error),
                "finishedAt": self._clock().isoformat(),
            }
            if stage != "audit":
                audit_run_id = self._record_failed_audit(
                    stage=stage,
                    started_at=started_at,
                    usage=usage,
                    error=failure["error"],
                )
                if audit_run_id is not None:
                    failure["auditRunId"] = audit_run_id
            if stage != "report":
                try:
                    self._report(failure)
                except Exception:  # noqa: BLE001
                    logger.warning("automation failure report could not be sent")
            raise RuntimeError(f"{stage} stage failed") from error
        finally:
            self._lock.release()

    def _record_audit(
        self,
        *,
        status: str,
        stage: str,
        started_at: datetime,
        finished_at: datetime,
        usage: HermesAnalysis,
        error: str | None = None,
        details: dict[str, object] | None = None,
    ) -> str | None:
        if self._audit is None:
            return None
        return self._audit(
            AutomationRunLog(
                run_type="daily",
                status=status,
                stage=stage,
                started_at=started_at,
                finished_at=finished_at,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                error=error,
                details=details,
            )
        )

    def _record_failed_audit(
        self,
        *,
        stage: str,
        started_at: datetime,
        usage: HermesAnalysis,
        error: object,
    ) -> str | None:
        try:
            return self._record_audit(
                status="failed",
                stage=stage,
                started_at=started_at,
                finished_at=self._clock(),
                usage=usage,
                error=str(error),
            )
        except Exception:  # noqa: BLE001
            logger.error("daily automation audit could not be recorded")
            return None


class PaperCycleProcess:
    def __init__(
        self, *, interval: str | None = None, timeout_seconds: int = 600
    ) -> None:
        if interval not in {None, "1m", "1d"}:
            raise ValueError("paper cycle interval must be 1m or 1d")
        self._interval = interval
        self._timeout_seconds = timeout_seconds

    def run(self) -> dict[str, Any]:
        environment = dict(os.environ)
        environment["TRADING_ENABLED"] = "false"
        command = [sys.executable, "-m", "toss_trader", "run-paper-cycle"]
        if self._interval is not None:
            command.extend(("--interval", self._interval))
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            env=environment,
            check=False,
        )
        output = completed.stdout.strip()
        error_output = completed.stderr.strip()
        payload = _load_json(output) if output else _load_json(error_output)
        if payload is None:
            payload = {
                "ok": False,
                "error": (error_output or output or "paper cycle produced no output")[
                    :4000
                ],
            }
        return {"exitCode": completed.returncode, "cycle": payload}


class IntradayPaperAutomation:
    def __init__(
        self,
        *,
        run_cycle: Callable[[], dict[str, Any]],
        report_notice: Callable[[dict[str, Any]], dict[str, Any]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._run_cycle = run_cycle
        self._report_notice = report_notice
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def run(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise AutomationBusy("intraday paper cycle is already running")
        try:
            cycle = self._run_cycle()
            notice = paper_cycle_notice(cycle)
            result: dict[str, Any] = {
                "ok": cycle.get("exitCode") == 0,
                "cycle": cycle,
                "notices": list(notice.lines) if notice is not None else [],
                "finishedAt": self._clock().isoformat(),
            }
            if notice is not None:
                result["noticeReported"] = self._report_notice(
                    {
                        "ok": result["ok"],
                        "cycle": cycle,
                        "analysis": "\n".join(notice.lines),
                        "severity": notice.severity,
                        "finishedAt": result["finishedAt"],
                    }
                )
            return result
        except AutomationBusy:
            raise
        except Exception as error:
            try:
                self._report_notice(
                    {
                        "ok": False,
                        "stage": "intraday-cycle",
                        "error": _safe_error(error),
                        "finishedAt": self._clock().isoformat(),
                    }
                )
            except Exception:  # noqa: BLE001
                logger.warning("intraday paper cycle failure could not be reported")
            raise RuntimeError("intraday-cycle stage failed") from error
        finally:
            self._lock.release()


class MarketScanProcess:
    def __init__(self, *, timeout_seconds: int = 600) -> None:
        self._timeout_seconds = timeout_seconds

    def run(self) -> dict[str, Any]:
        environment = dict(os.environ)
        environment["TRADING_ENABLED"] = "false"
        completed = subprocess.run(
            [sys.executable, "-m", "toss_trader", "run-market-scan"],
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            env=environment,
            check=False,
        )
        output = completed.stdout.strip()
        error_output = completed.stderr.strip()
        payload = _load_json(output) if output else _load_json(error_output)
        if payload is None:
            payload = {
                "markets": [],
                "candidates": [],
                "errors": {"process": (error_output or output or "no output")[:4000]},
            }
        return {"exitCode": completed.returncode, "scan": payload}


class MarketScanAutomation:
    def __init__(
        self,
        *,
        run_scan: Callable[[], dict[str, Any]],
        analyze: Callable[[dict[str, Any]], str | HermesAnalysis],
        report: Callable[[dict[str, Any]], dict[str, Any]],
        audit: Callable[[AutomationRunLog], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._run_scan = run_scan
        self._analyze = analyze
        self._report = report
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def run(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise AutomationBusy("market scan automation is already running")
        started_at = self._clock()
        usage = HermesAnalysis(content="")
        stage = "scan"
        try:
            scan = self._run_scan()
            scan_payload = scan.get("scan")
            if not isinstance(scan_payload, dict):
                raise TypeError("market scan process returned invalid JSON")
            stage = "hermes"
            usage = _hermes_analysis(self._analyze(scan_payload))
            opinion = usage.content
            stage = "report"
            result = {
                "ok": True,
                "scan": scan,
                "opinion": opinion,
                "analysis": format_market_scan_report(scan, opinion=opinion),
                "hermesUsage": _hermes_usage(usage),
                "finishedAt": self._clock().isoformat(),
            }
            reported = self._report(result)
            result = {**result, "reported": reported}
            stage = "audit"
            audit_run_id = self._record_audit(
                status="succeeded",
                stage="completed",
                started_at=started_at,
                finished_at=self._clock(),
                usage=usage,
                details=_market_run_details(scan),
            )
            if audit_run_id is not None:
                result["auditRunId"] = audit_run_id
            return result
        except AutomationBusy:
            raise
        except Exception as error:
            failure = {
                "ok": False,
                "stage": stage,
                "error": _safe_error(error),
                "finishedAt": self._clock().isoformat(),
            }
            if stage != "audit":
                audit_run_id = self._record_failed_audit(
                    stage=stage,
                    started_at=started_at,
                    usage=usage,
                    error=failure["error"],
                )
                if audit_run_id is not None:
                    failure["auditRunId"] = audit_run_id
            if stage != "report":
                try:
                    self._report(failure)
                except Exception:  # noqa: BLE001
                    logger.warning("market scan failure report could not be sent")
            raise RuntimeError(f"{stage} stage failed") from error
        finally:
            self._lock.release()

    def _record_audit(
        self,
        *,
        status: str,
        stage: str,
        started_at: datetime,
        finished_at: datetime,
        usage: HermesAnalysis,
        error: str | None = None,
        details: dict[str, object] | None = None,
    ) -> str | None:
        if self._audit is None:
            return None
        return self._audit(
            AutomationRunLog(
                run_type="market_scan",
                status=status,
                stage=stage,
                started_at=started_at,
                finished_at=finished_at,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                error=error,
                details=details,
            )
        )

    def _record_failed_audit(
        self,
        *,
        stage: str,
        started_at: datetime,
        usage: HermesAnalysis,
        error: object,
    ) -> str | None:
        try:
            return self._record_audit(
                status="failed",
                stage=stage,
                started_at=started_at,
                finished_at=self._clock(),
                usage=usage,
                error=str(error),
            )
        except Exception:  # noqa: BLE001
            logger.error("market scan automation audit could not be recorded")
            return None


class HermesAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "http://hermes:8642",
        timeout_seconds: int = 300,
        system_prompt: str | None = None,
    ) -> None:
        if len(api_key) < 16:
            raise ValueError("HERMES_API_KEY must contain at least 16 characters")
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._timeout_seconds = timeout_seconds
        self._system_prompt = system_prompt or (
            "너는 Toss Trader paper trading 운영 분석기다. "
            "제공된 JSON만 분석하고 도구를 호출하지 마라. "
            "한국어 평문 6줄 이내로 결과, 신호, 체결, 실패, "
            "수익률, 다음 확인사항을 요약하라. 매매 추천은 하지 마라."
        )

    def analyze(self, cycle: dict[str, Any]) -> HermesAnalysis:
        cycle_json = json.dumps(cycle, ensure_ascii=False, default=str)
        request_body = {
            "model": "hermes-agent",
            "stream": False,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt,
                },
                {"role": "user", "content": cycle_json[:65_536]},
            ],
        }
        request = urllib.request.Request(
            self._url,
            data=json.dumps(request_body, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                payload = json.loads(response.read())
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
            raise RuntimeError("Hermes API request failed") from error
        try:
            content = payload["choices"][0]["message"]["content"].strip()
        except (AttributeError, IndexError, KeyError, TypeError) as error:
            raise RuntimeError("Hermes API response is missing content") from error
        if not content:
            raise RuntimeError("Hermes API returned empty content")
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = _token_count(usage.get("prompt_tokens"))
        completion_tokens = _token_count(usage.get("completion_tokens"))
        total_tokens = max(
            _token_count(usage.get("total_tokens")),
            prompt_tokens + completion_tokens,
        )
        return HermesAnalysis(
            content=content[:4000],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


class AlertmanagerReporter:
    def __init__(
        self,
        *,
        url: str = "http://alertmanager:9093/api/v2/alerts",
        timeout_seconds: int = 10,
        clock: Callable[[], datetime] | None = None,
        alert_name: str = "TossTraderDailyReport",
        summary: str = "Toss Trader paper daily report",
    ) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._alert_name = alert_name
        self._summary = summary

    def report(self, result: dict[str, Any]) -> dict[str, Any]:
        now = self._clock()
        ok = bool(result.get("ok"))
        job = result.get("cycle") or result.get("scan")
        job = job if isinstance(job, dict) else {}
        exit_code = job.get("exitCode")
        severity = "info" if ok and exit_code == 0 else "warning"
        if not ok:
            severity = "critical"
        requested_severity = result.get("severity")
        if requested_severity in {"info", "warning", "critical"}:
            severity = str(requested_severity)
        if ok:
            description = str(result.get("analysis", ""))
        elif result.get("stage") == "hermes":
            description = f"Hermes 분석 실패\n{result.get('error', 'failed')}"
        else:
            description = (
                f"{result.get('stage', 'unknown')}: "
                f"{result.get('error', 'failed')}"
            )
        alert = [
            {
                "labels": {
                    "alertname": self._alert_name,
                    "severity": severity,
                    "service": "toss-trader",
                },
                "annotations": {
                    "summary": self._summary,
                    "description": html.escape(description[:4000]),
                },
                "startsAt": _utc_iso(now),
                "endsAt": _utc_iso(now + timedelta(minutes=2)),
                "generatorURL": "http://prometheus:9090",
            }
        ]
        request = urllib.request.Request(
            self._url,
            data=json.dumps(alert, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                response.read()
        except (OSError, urllib.error.HTTPError) as error:
            raise RuntimeError("Alertmanager report failed") from error
        return {"accepted": True, "severity": severity}


def create_daily_automation_from_env() -> DailyAutomation:
    api_key = os.environ.get("HERMES_API_KEY", "")
    alertmanager_url = os.environ.get(
        "ALERTMANAGER_API_URL",
        "http://alertmanager:9093/api/v2/alerts",
    )
    return DailyAutomation(
        run_cycle=PaperCycleProcess().run,
        analyze=HermesAnalyzer(
            api_key=api_key,
            base_url=os.environ.get(
                "HERMES_API_BASE_URL", "http://hermes-analysis:8642"
            ),
        ).analyze,
        report=AlertmanagerReporter(
            url=alertmanager_url,
        ).report,
        report_notice=AlertmanagerReporter(
            url=alertmanager_url,
            alert_name="TossTraderPaperCycleNotice",
            summary="Toss Trader paper cycle 특이사항",
        ).report,
        audit=_record_automation_run_from_env,
    )


def create_intraday_paper_automation_from_env() -> IntradayPaperAutomation:
    return IntradayPaperAutomation(
        run_cycle=PaperCycleProcess(interval="1m").run,
        report_notice=AlertmanagerReporter(
            url=os.environ.get(
                "ALERTMANAGER_API_URL",
                "http://alertmanager:9093/api/v2/alerts",
            ),
            alert_name="TossTraderPaperCycleNotice",
            summary="Toss Trader 장중 paper cycle 특이사항",
        ).report,
    )


def create_market_scan_automation_from_env() -> MarketScanAutomation:
    api_key = os.environ.get("HERMES_API_KEY", "")
    return MarketScanAutomation(
        run_scan=MarketScanProcess().run,
        analyze=HermesAnalyzer(
            api_key=api_key,
            base_url=os.environ.get(
                "HERMES_API_BASE_URL", "http://hermes-analysis:8642"
            ),
            system_prompt=(
                "너는 한국 주식시장 장전 리포트 분석가다. 제공된 JSON의 시장 "
                "상태, 20일 모멘텀, 거래량 비율, 발굴 후보를 함께 비교해 맥락을 "
                "해석하라. 단순히 RISK_ON/NEUTRAL/RISK_OFF를 되풀이하지 말고 "
                "시장 간 엇갈림, 후보 강도, 주의점을 판단하라. 한국어 2~4문장, "
                "500자 이내로 작성하라. 확정적 수익 표현과 직접적인 매수·매도 "
                "지시는 금지한다. 도구를 호출하지 말고 제공된 JSON만 사용하라."
            ),
        ).analyze,
        report=AlertmanagerReporter(
            url=os.environ.get(
                "ALERTMANAGER_API_URL",
                "http://alertmanager:9093/api/v2/alerts",
            ),
            alert_name="TossTraderMarketScan",
            summary="Toss Trader 시장분석·종목발굴",
        ).report,
        audit=_record_automation_run_from_env,
    )


def automation_response(
    method: str,
    path: str,
    service: DailyAutomation,
    *,
    market_service: MarketScanAutomation | None = None,
    intraday_service: IntradayPaperAutomation | None = None,
) -> tuple[int, dict[str, Any]]:
    normalized = urlsplit(path).path
    if normalized == "/healthz":
        if method != "GET":
            return 405, {"ok": False, "error": "method not allowed"}
        return 200, {"status": "ok"}
    if normalized == "/run-daily":
        selected_service: (
            DailyAutomation | MarketScanAutomation | IntradayPaperAutomation
        ) = service
    elif normalized == "/run-market-scan" and market_service is not None:
        selected_service = market_service
    elif normalized == "/run-paper-cycle" and intraday_service is not None:
        selected_service = intraday_service
    else:
        return 404, {"ok": False, "error": "not found"}
    if method != "POST":
        return 405, {"ok": False, "error": "method not allowed"}
    try:
        return 200, selected_service.run()
    except AutomationBusy as error:
        return 409, {"ok": False, "error": str(error)}
    except RuntimeError as error:
        return 502, {"ok": False, "error": str(error)}


def serve_automation(
    *,
    host: str,
    port: int,
    service: DailyAutomation,
    market_service: MarketScanAutomation | None = None,
    intraday_service: IntradayPaperAutomation | None = None,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond("GET")

        def do_POST(self) -> None:
            self._respond("POST")

        def _respond(self, method: str) -> None:
            status, payload = automation_response(
                method,
                self.path,
                service,
                market_service=market_service,
                intraday_service=intraday_service,
            )
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _load_json(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _record_automation_run_from_env(run: AutomationRunLog) -> str:
    settings = Settings.from_env()
    ledger = open_paper_ledger(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    try:
        return ledger.record_automation_run(
            run_type=run.run_type,
            status=run.status,
            stage=run.stage,
            started_at=run.started_at,
            finished_at=run.finished_at,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            total_tokens=run.total_tokens,
            error=run.error,
            details=run.details,
        )
    finally:
        ledger.close()


def _hermes_analysis(value: str | HermesAnalysis) -> HermesAnalysis:
    if isinstance(value, HermesAnalysis):
        return value
    if not isinstance(value, str):
        raise TypeError("Hermes analyzer returned invalid result")
    content = value.strip()
    if not content:
        raise RuntimeError("Hermes analyzer returned empty content")
    return HermesAnalysis(content=content[:4000])


def _hermes_usage(analysis: HermesAnalysis) -> dict[str, int]:
    return {
        "promptTokens": analysis.prompt_tokens,
        "completionTokens": analysis.completion_tokens,
        "totalTokens": analysis.total_tokens,
    }


def _token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _daily_run_details(cycle: dict[str, Any]) -> dict[str, object]:
    payload = cycle.get("cycle")
    payload = payload if isinstance(payload, dict) else {}
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    return {
        "exitCode": cycle.get("exitCode"),
        "symbols": _non_negative_int(summary.get("symbols")),
        "signals": _non_negative_int(summary.get("signals")),
        "fills": _non_negative_int(summary.get("fills")),
        "failed": _non_negative_int(summary.get("failed")),
    }


def _market_run_details(scan: dict[str, Any]) -> dict[str, object]:
    payload = scan.get("scan")
    payload = payload if isinstance(payload, dict) else {}
    markets = payload.get("markets")
    candidates = payload.get("candidates")
    errors = payload.get("errors")
    return {
        "exitCode": scan.get("exitCode"),
        "markets": len(markets) if isinstance(markets, list) else 0,
        "candidates": len(candidates) if isinstance(candidates, list) else 0,
        "errors": len(errors) if isinstance(errors, dict) else 0,
    }


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _higher_severity(current: str, candidate: str) -> str:
    rank = {"info": 0, "warning": 1, "critical": 2}
    return candidate if rank[candidate] > rank[current] else current


def _safe_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:500] or type(error).__name__


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
