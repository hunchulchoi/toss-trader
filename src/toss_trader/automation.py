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
from .models import Side, TradeSignal
from .paper import open_paper_ledger
from .risk import (
    RiskContext,
    RiskLimits,
    RiskManager,
    UniverseCandidateRisk,
    UniverseRiskContext,
)
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

    portfolios = cycle.get("portfolios")
    if isinstance(portfolios, dict):
        for portfolio_id, portfolio_cycle in portfolios.items():
            if not isinstance(portfolio_cycle, dict):
                continue
            nested = paper_cycle_notice(
                {
                    "exitCode": 0,
                    "cycle": portfolio_cycle,
                }
            )
            if nested is None:
                continue
            label = "규칙 기반" if portfolio_id == "rule" else "Hermes 개입"
            lines.extend(f"[{label}] {line}" for line in nested.lines)
            severity = _higher_severity(severity, nested.severity)

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
                quantity = _grouped_number(fill.get("quantity"))
                price = _grouped_number(fill.get("price"))
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
                    if {"daily-loss-limit", "api-error-kill-switch"} & set(noteworthy)
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
        portfolios: dict[str, Any] = {}
        exit_code = 0
        for portfolio_id in ("rule", "hermes"):
            command = [
                sys.executable,
                "-m",
                "toss_trader",
                "run-paper-cycle",
                "--portfolio",
                portfolio_id,
            ]
            if portfolio_id == "hermes":
                command.append("--hermes-advisor")
                rule_cycle = portfolios.get("rule")
                rule_universe = (
                    rule_cycle.get("universe") if isinstance(rule_cycle, dict) else None
                )
                if isinstance(rule_universe, dict):
                    symbols = rule_universe.get("symbols")
                    entry_symbols = rule_universe.get("entrySymbols")
                    run_id = rule_universe.get("runId")
                    if isinstance(symbols, list) and symbols:
                        command.extend(
                            ("--symbols", *(str(value) for value in symbols))
                        )
                    if isinstance(entry_symbols, list) and entry_symbols:
                        command.extend(
                            (
                                "--trend-entry-symbols",
                                *(str(value) for value in entry_symbols),
                            )
                        )
                    if run_id:
                        command.extend(("--trend-entry-key", str(run_id)))
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
                    "error": (
                        error_output or output or "paper cycle produced no output"
                    )[:4000],
                }
            portfolios[portfolio_id] = payload
            exit_code = max(exit_code, completed.returncode)
        return {
            "exitCode": exit_code,
            "cycle": {
                "comparison": True,
                "interval": self._interval or "1d",
                "portfolios": portfolios,
                "summary": {
                    "symbols": sum(
                        int(value.get("summary", {}).get("symbols", 0))
                        for value in portfolios.values()
                        if isinstance(value, dict)
                    ),
                    "signals": sum(
                        int(value.get("summary", {}).get("signals", 0))
                        for value in portfolios.values()
                        if isinstance(value, dict)
                    ),
                    "fills": sum(
                        int(value.get("summary", {}).get("fills", 0))
                        for value in portfolios.values()
                        if isinstance(value, dict)
                    ),
                    "skipped": sum(
                        int(value.get("summary", {}).get("skipped", 0))
                        for value in portfolios.values()
                        if isinstance(value, dict)
                    ),
                    "failed": sum(
                        int(value.get("summary", {}).get("failed", 0))
                        for value in portfolios.values()
                        if isinstance(value, dict)
                    ),
                },
            },
        }


class PaperPortfolioProcess:
    def __init__(self, *, timeout_seconds: int = 600) -> None:
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        portfolio_id: str,
        interval: str,
        rule_cycle: dict[str, Any] | None = None,
        workflow_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if portfolio_id not in {"rule", "hermes"}:
            raise ValueError("workflow paper portfolio must be rule or hermes")
        if interval not in {"1m", "1d"}:
            raise ValueError("workflow paper interval must be 1m or 1d")
        command = [
            sys.executable,
            "-m",
            "toss_trader",
            "run-paper-cycle",
            "--portfolio",
            portfolio_id,
            "--interval",
            interval,
        ]
        snapshot_input: str | None = None
        if portfolio_id == "hermes":
            command.extend(("--hermes-advisor", "--snapshot-stdin"))
            snapshot_input = json.dumps(
                _rule_shared_snapshot(rule_cycle),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        environment = dict(os.environ)
        environment["TRADING_ENABLED"] = "false"
        context = workflow_context if isinstance(workflow_context, dict) else {}
        execution_id = context.get("executionId")
        if isinstance(execution_id, (str, int)):
            environment["N8N_PARENT_EXECUTION_ID"] = str(execution_id)
        environment["N8N_PARENT_WORKFLOW_ID"] = str(
            context.get("workflowId") or "unknown"
        )
        environment["PAPER_PORTFOLIO_ID"] = portfolio_id
        environment["PAPER_INTERVAL"] = interval
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            input=snapshot_input,
            timeout=self._timeout_seconds,
            env=environment,
            check=False,
        )
        raw = completed.stdout.strip() or completed.stderr.strip()
        payload = _load_json(raw)
        if payload is None:
            payload = {
                "ok": False,
                "error": (raw or "paper cycle produced no output")[:4000],
            }
        return {"exitCode": completed.returncode, "cycle": payload}


def _rule_shared_snapshot(rule_cycle: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(rule_cycle, dict):
        raise TypeError("Hermes paper task requires rule cycle JSON")
    cycle = rule_cycle.get("cycle")
    cycle = cycle if isinstance(cycle, dict) else rule_cycle
    snapshot = cycle.get("sharedSnapshot")
    if not isinstance(snapshot, dict):
        raise TypeError("rule cycle is missing shared market snapshot")
    if snapshot.get("version") != 1:
        raise ValueError("rule cycle snapshot version is unsupported")
    return snapshot


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
        return _hermes_analysis_from_response(payload)


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
        analysis = result.get("analysis")
        if ok:
            description = str(result.get("analysis", ""))
        elif isinstance(analysis, str) and analysis.strip():
            description = analysis
        elif result.get("stage") == "hermes":
            description = f"Hermes 분석 실패\n{result.get('error', 'failed')}"
        else:
            description = (
                f"{result.get('stage', 'unknown')}: {result.get('error', 'failed')}"
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


class WorkflowTaskService:
    def __init__(
        self,
        *,
        paper: PaperPortfolioProcess,
        market_scan: MarketScanProcess,
        market_analyzer: HermesAnalyzer,
        daily_analyzer: HermesAnalyzer,
        market_reporter: AlertmanagerReporter,
        paper_reporter: AlertmanagerReporter,
        daily_reporter: AlertmanagerReporter,
        failure_reporter: AlertmanagerReporter,
        audit: Callable[[AutomationRunLog], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._paper = paper
        self._market_scan = market_scan
        self._market_analyzer = market_analyzer
        self._daily_analyzer = daily_analyzer
        self._market_reporter = market_reporter
        self._paper_reporter = paper_reporter
        self._daily_reporter = daily_reporter
        self._failure_reporter = failure_reporter
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        started_at = self._clock()
        try:
            result = self._dispatch(path, payload)
        except Exception as error:
            self._audit_flow(path, payload, started_at, error=error)
            raise
        self._audit_flow(path, payload, started_at, result=result)
        return result

    def _dispatch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/workflow/market-scan":
            return self._market_scan.run()
        if path == "/workflow/paper-rule-1m":
            return self._paper.run(
                portfolio_id="rule",
                interval="1m",
                workflow_context=payload.get("_workflow"),
            )
        if path == "/workflow/paper-rule-1d":
            return self._paper.run(
                portfolio_id="rule",
                interval="1d",
                workflow_context=payload.get("_workflow"),
            )
        if path == "/workflow/paper-hermes-1m":
            return self._paper.run(
                portfolio_id="hermes",
                interval="1m",
                rule_cycle=payload.get("rule"),
                workflow_context=payload.get("_workflow"),
            )
        if path == "/workflow/paper-hermes-1d":
            return self._paper.run(
                portfolio_id="hermes",
                interval="1d",
                rule_cycle=payload.get("rule"),
                workflow_context=payload.get("_workflow"),
            )
        if path == "/workflow/hermes-market":
            return self._analyze_market(payload)
        if path == "/workflow/hermes-daily":
            return self._analyze_daily(payload)
        if path == "/workflow/hermes-market-result":
            return self._complete_market(payload)
        if path == "/workflow/hermes-daily-result":
            return self._complete_daily(payload)
        if path == "/workflow/report-market":
            return self._market_reporter.report(_required_result(payload))
        if path == "/workflow/report-paper":
            return self._report_paper(payload)
        if path == "/workflow/report-daily":
            return self._daily_reporter.report(_required_result(payload))
        if path == "/workflow/report-failure":
            failure = _workflow_failure(payload)
            return self._failure_reporter.report(
                {
                    "ok": False,
                    "stage": "n8n-workflow",
                    "analysis": failure["message"],
                    "severity": failure["severity"],
                }
            )
        if path == "/workflow/risk-manager-evaluate":
            return _evaluate_risk_payload(payload)
        if path == "/workflow/risk-manager-audit":
            return _risk_audit_payload(payload)
        raise ValueError("unknown workflow task")

    def _audit_flow(
        self,
        path: str,
        payload: dict[str, Any],
        started_at: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        if self._audit is None:
            return
        context = payload.get("_workflow")
        context = context if isinstance(context, dict) else {}
        finished_at = self._clock()
        status = "failed" if error is not None else _flow_status(context, result)
        usage = _flow_usage(result)
        self._audit(
            AutomationRunLog(
                run_type="n8n_flow",
                status=status,
                stage=str(context.get("stage") or path.removeprefix("/workflow/")),
                started_at=started_at,
                finished_at=finished_at,
                prompt_tokens=usage[0],
                completion_tokens=usage[1],
                total_tokens=usage[2],
                error=_safe_error(error) if error is not None else None,
                details=_flow_audit_details(path, context, payload, result),
            )
        )

    def _analyze_market(self, payload: dict[str, Any]) -> dict[str, Any]:
        scan = payload.get("scan")
        scan = scan.get("scan") if isinstance(scan, dict) else None
        if not isinstance(scan, dict):
            raise TypeError("market workflow is missing scan JSON")
        started_at = self._clock()
        try:
            usage = self._market_analyzer.analyze(scan)
        except Exception as error:
            self._audit_failure("market_scan", started_at, error)
            raise
        result = {
            "ok": True,
            "scan": payload["scan"],
            "opinion": usage.content,
            "analysis": format_market_scan_report(
                payload["scan"], opinion=usage.content
            ),
            "hermesUsage": _hermes_usage(usage),
            "finishedAt": self._clock().isoformat(),
        }
        self._audit_usage("market_scan", started_at, usage, result)
        return result

    def _analyze_daily(self, payload: dict[str, Any]) -> dict[str, Any]:
        comparison = _comparison_payload(payload)
        started_at = self._clock()
        try:
            usage = self._daily_analyzer.analyze(comparison)
        except Exception as error:
            self._audit_failure("daily", started_at, error)
            raise
        result = {
            "ok": True,
            "cycle": comparison,
            "analysis": usage.content,
            "hermesUsage": _hermes_usage(usage),
            "finishedAt": self._clock().isoformat(),
        }
        self._audit_usage("daily", started_at, usage, result)
        return result

    def _complete_market(self, payload: dict[str, Any]) -> dict[str, Any]:
        scan = payload.get("scan")
        if not isinstance(scan, dict) or not isinstance(scan.get("scan"), dict):
            raise TypeError("market workflow is missing scan JSON")
        started_at = self._clock()
        try:
            usage = _hermes_analysis_from_response(payload.get("hermesResponse"))
        except Exception as error:
            self._audit_failure("market_scan", started_at, error)
            raise
        result = {
            "ok": True,
            "scan": scan,
            "opinion": usage.content,
            "analysis": format_market_scan_report(scan, opinion=usage.content),
            "hermesUsage": _hermes_usage(usage),
            "finishedAt": self._clock().isoformat(),
        }
        self._audit_usage("market_scan", started_at, usage, result)
        return result

    def _complete_daily(self, payload: dict[str, Any]) -> dict[str, Any]:
        comparison = _comparison_payload(payload)
        started_at = self._clock()
        try:
            usage = _hermes_analysis_from_response(payload.get("hermesResponse"))
        except Exception as error:
            self._audit_failure("daily", started_at, error)
            raise
        result = {
            "ok": True,
            "cycle": comparison,
            "analysis": usage.content,
            "hermesUsage": _hermes_usage(usage),
            "finishedAt": self._clock().isoformat(),
        }
        self._audit_usage("daily", started_at, usage, result)
        return result

    def _report_paper(self, payload: dict[str, Any]) -> dict[str, Any]:
        comparison = _comparison_payload(payload)
        notice = paper_cycle_notice(comparison)
        if notice is None:
            return {"accepted": False, "skipped": True, "reason": "no-notice"}
        return self._paper_reporter.report(
            {
                "ok": comparison.get("exitCode") == 0,
                "cycle": comparison,
                "analysis": "\n".join(notice.lines),
                "severity": notice.severity,
            }
        )

    def _audit_usage(
        self,
        run_type: str,
        started_at: datetime,
        usage: HermesAnalysis,
        result: dict[str, Any],
    ) -> None:
        if self._audit is None:
            return
        self._audit(
            AutomationRunLog(
                run_type=run_type,
                status="succeeded",
                stage="completed",
                started_at=started_at,
                finished_at=self._clock(),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                details={"orchestrator": "n8n", "ok": bool(result.get("ok"))},
            )
        )

    def _audit_failure(
        self, run_type: str, started_at: datetime, error: Exception
    ) -> None:
        if self._audit is None:
            return
        self._audit(
            AutomationRunLog(
                run_type=run_type,
                status="failed",
                stage="hermes",
                started_at=started_at,
                finished_at=self._clock(),
                error=_safe_error(error),
                details={"orchestrator": "n8n"},
            )
        )


def create_workflow_task_service_from_env() -> WorkflowTaskService:
    api_key = os.environ.get("HERMES_API_KEY", "")
    base_url = os.environ.get("HERMES_API_BASE_URL", "http://hermes-analysis:8642")
    alertmanager_url = os.environ.get(
        "ALERTMANAGER_API_URL", "http://alertmanager:9093/api/v2/alerts"
    )
    return WorkflowTaskService(
        paper=PaperPortfolioProcess(),
        market_scan=MarketScanProcess(),
        market_analyzer=HermesAnalyzer(
            api_key=api_key,
            base_url=base_url,
            system_prompt=(
                "너는 한국 주식시장 장전 리포트 분석가다. 제공된 JSON만 해석하라. "
                "한국어 2~4문장으로 시장 간 엇갈림, 모멘텀, 거래량, 후보 강도와 "
                "주의점을 설명하라. 직접 매수·매도 지시와 수익 보장은 금지한다."
            ),
        ),
        daily_analyzer=HermesAnalyzer(api_key=api_key, base_url=base_url),
        market_reporter=AlertmanagerReporter(
            url=alertmanager_url,
            alert_name="TossTraderMarketScan",
            summary="Toss Trader 시장분석·종목발굴",
        ),
        paper_reporter=AlertmanagerReporter(
            url=alertmanager_url,
            alert_name="TossTraderPaperCycleNotice",
            summary="Toss Trader 장중 paper cycle 특이사항",
        ),
        daily_reporter=AlertmanagerReporter(url=alertmanager_url),
        failure_reporter=AlertmanagerReporter(
            url=alertmanager_url,
            alert_name="TossTraderWorkflowFailure",
            summary="Toss Trader n8n workflow 실패",
        ),
        audit=_record_automation_run_from_env,
    )


def _comparison_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rule = payload.get("rule")
    hermes = payload.get("hermes")
    if not isinstance(rule, dict) or not isinstance(hermes, dict):
        raise TypeError("comparison workflow requires rule and hermes JSON")
    portfolios = {
        "rule": rule.get("cycle", rule),
        "hermes": hermes.get("cycle", hermes),
    }
    summaries = [
        value.get("summary", {})
        for value in portfolios.values()
        if isinstance(value, dict)
    ]
    return {
        "exitCode": max(int(rule.get("exitCode", 1)), int(hermes.get("exitCode", 1))),
        "cycle": {
            "comparison": True,
            "portfolios": portfolios,
            "summary": {
                key: sum(int(summary.get(key, 0)) for summary in summaries)
                for key in ("symbols", "signals", "fills", "skipped", "failed")
            },
        },
    }


def _evaluate_risk_payload(payload: dict[str, Any]) -> dict[str, object]:
    manager = RiskManager(RiskLimits())
    kind = payload.get("kind")
    if kind == "trade":
        signal_payload = _required_mapping(payload, "signal")
        context_payload = _required_mapping(payload, "context")
        signal = TradeSignal(
            signal_id=_required_text(signal_payload, "signalId"),
            symbol=_required_text(signal_payload, "symbol"),
            side=Side(_required_text(signal_payload, "side")),
            reference_price=_required_decimal(signal_payload, "referencePrice"),
            quantity=_required_decimal(signal_payload, "quantity"),
            reason=_required_text(signal_payload, "reason"),
        )
        context = RiskContext(
            now=_required_datetime(context_payload, "now"),
            market_close_at=_optional_datetime(context_payload.get("marketCloseAt")),
            market_is_business_day=_required_bool(
                context_payload, "marketIsBusinessDay"
            ),
            position_notional=_required_decimal(context_payload, "positionNotional"),
            position_quantity=_required_decimal(context_payload, "positionQuantity"),
            available_cash=_optional_decimal(context_payload.get("availableCash")),
            daily_buy_count=_required_int(context_payload, "dailyBuyCount"),
            open_position_count=_required_int(context_payload, "openPositionCount"),
            daily_return_rate=_required_decimal(context_payload, "dailyReturnRate"),
            consecutive_api_errors=_required_int(
                context_payload, "consecutiveApiErrors"
            ),
            seen_signal_ids=frozenset(
                _required_text_list(context_payload, "seenSignalIds")
            ),
            new_buys_allowed=_required_bool(context_payload, "newBuysAllowed"),
            advisor_status=_optional_text(context_payload.get("advisorStatus")),
            advisor_rationale=_optional_text(context_payload.get("advisorRationale")),
        )
        decision = manager.evaluate(signal, context)
    elif kind == "universe":
        candidate_payload = _required_mapping(payload, "candidate")
        context_payload = _required_mapping(payload, "context")
        candidate = UniverseCandidateRisk(
            symbol=_required_text(candidate_payload, "symbol"),
            reference_price=_required_decimal(candidate_payload, "referencePrice"),
            security_type=_required_text(candidate_payload, "securityType"),
            is_common_share=_required_bool(candidate_payload, "isCommonShare"),
            status=_required_text(candidate_payload, "status"),
            trading_suspended=_required_bool(candidate_payload, "tradingSuspended"),
        )
        context = UniverseRiskContext(
            quantity=_required_decimal(context_payload, "quantity"),
            available_cash=_required_decimal(context_payload, "availableCash"),
            daily_return_rate=_required_decimal(context_payload, "dailyReturnRate"),
            consecutive_api_errors=_required_int(
                context_payload, "consecutiveApiErrors"
            ),
        )
        decision = manager.evaluate_universe_candidate(candidate, context)
    else:
        raise ValueError("RiskManager workflow kind must be trade or universe")
    return {
        "ok": True,
        "approved": decision.approved,
        "violations": list(decision.violations),
    }


def _risk_audit_payload(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "approved": _required_bool(payload, "approved"),
        "violations": _required_text_list(payload, "violations"),
    }


def _required_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"RiskManager {key} must be an object")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"RiskManager {key} must be non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("RiskManager optional text is invalid")
    return value


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"RiskManager {key} must be boolean")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"RiskManager {key} must be a non-negative integer")
    return value


def _required_text_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"RiskManager {key} must be a text list")
    return value


def _required_decimal(payload: dict[str, Any], key: str) -> Decimal:
    value = _decimal(payload.get(key))
    if value is None:
        raise TypeError(f"RiskManager {key} must be decimal")
    return value


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    parsed = _decimal(value)
    if parsed is None:
        raise TypeError("RiskManager optional decimal is invalid")
    return parsed


def _required_datetime(payload: dict[str, Any], key: str) -> datetime:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"RiskManager {key} must be datetime text")
    parsed = _optional_datetime(value)
    if parsed is None:
        raise TypeError(f"RiskManager {key} must be datetime text")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("RiskManager datetime is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("RiskManager datetime must include timezone")
    return parsed


def _required_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TypeError("workflow report requires result JSON")
    return result


def automation_response(
    method: str,
    path: str,
    service: DailyAutomation,
    *,
    market_service: MarketScanAutomation | None = None,
    intraday_service: IntradayPaperAutomation | None = None,
    workflow_service: WorkflowTaskService | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    normalized = urlsplit(path).path
    if normalized == "/healthz":
        if method != "GET":
            return 405, {"ok": False, "error": "method not allowed"}
        return 200, {"status": "ok"}
    if normalized.startswith("/workflow/") and workflow_service is not None:
        if method != "POST":
            return 405, {"ok": False, "error": "method not allowed"}
        try:
            return 200, workflow_service.run(normalized, payload or {})
        except AutomationBusy as error:
            return 409, {"ok": False, "error": str(error)}
        except (RuntimeError, TypeError, ValueError) as error:
            return 502, {"ok": False, "error": str(error)}
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
    workflow_service: WorkflowTaskService | None = None,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond("GET")

        def do_POST(self) -> None:
            self._respond("POST")

        def _respond(self, method: str) -> None:
            payload: dict[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    raw = self.rfile.read(min(length, 1_048_576))
                    try:
                        decoded = json.loads(raw)
                    except json.JSONDecodeError:
                        decoded = None
                    if not isinstance(decoded, dict):
                        self._write(400, {"ok": False, "error": "invalid JSON body"})
                        return
                    payload = decoded
            status, payload = automation_response(
                method,
                self.path,
                service,
                market_service=market_service,
                intraday_service=intraday_service,
                workflow_service=workflow_service,
                payload=payload,
            )
            self._write(status, payload)

        def _write(self, status: int, payload: dict[str, Any]) -> None:
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
        initialize_schema=False,
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


def _flow_status(context: dict[str, Any], result: dict[str, Any] | None) -> str:
    requested = context.get("status")
    if requested in {"failed", "skipped"}:
        return str(requested)
    if isinstance(result, dict) and result.get("skipped") is True:
        return "skipped"
    if isinstance(result, dict) and result.get("ok") is False:
        return "failed"
    return "succeeded"


def _flow_usage(result: dict[str, Any] | None) -> tuple[int, int, int]:
    usage = result.get("hermesUsage") if isinstance(result, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt = _token_count(usage.get("promptTokens"))
    completion = _token_count(usage.get("completionTokens"))
    total = max(_token_count(usage.get("totalTokens")), prompt + completion)
    return prompt, completion, total


def _flow_audit_details(
    path: str,
    context: dict[str, Any],
    payload: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "orchestrator": "n8n",
        "endpoint": path,
    }
    for source, target in (
        ("workflowId", "workflowId"),
        ("executionId", "executionId"),
        ("trigger", "trigger"),
        ("portfolioId", "portfolioId"),
        ("interval", "interval"),
        ("parentExecutionId", "parentExecutionId"),
    ):
        value = context.get(source)
        if isinstance(value, (str, int, float, bool)):
            details[target] = value
    if isinstance(result, dict):
        for source, target in (
            ("exitCode", "exitCode"),
            ("accepted", "telegramAccepted"),
            ("skipped", "skipped"),
            ("reason", "reason"),
        ):
            value = result.get(source)
            if isinstance(value, (str, int, float, bool)):
                details[target] = value
        cycle = result.get("cycle")
        cycle = cycle if isinstance(cycle, dict) else {}
        summary = cycle.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        for key in ("symbols", "signals", "fills", "skipped", "failed"):
            if key in summary:
                details[key] = _non_negative_int(summary.get(key))
        decisions = sorted(set(_collect_decision_ids(result)))
        if decisions:
            details["riskDecisionIds"] = decisions[:100]
        if path in (
            "/workflow/risk-manager-evaluate",
            "/workflow/risk-manager-audit",
        ):
            details["approved"] = bool(result.get("approved"))
            violations = result.get("violations")
            if isinstance(violations, list) and all(
                isinstance(value, str) for value in violations
            ):
                details["violations"] = violations
    if path in (
        "/workflow/risk-manager-evaluate",
        "/workflow/risk-manager-audit",
    ):
        kind = payload.get("kind")
        if isinstance(kind, str):
            details["riskKind"] = kind
        entity = payload.get("signal") if kind == "trade" else payload.get("candidate")
        if isinstance(entity, dict) and isinstance(entity.get("symbol"), str):
            details["symbol"] = entity["symbol"]
        elif isinstance(payload.get("symbol"), str):
            details["symbol"] = payload["symbol"]
    if path == "/workflow/report-failure":
        details["failure"] = _workflow_failure(payload)["details"]
    return details


def _workflow_failure(payload: dict[str, Any]) -> dict[str, object]:
    """Build a Telegram-safe failure summary and a compact audit record."""
    context = payload.get("_workflow")
    context = context if isinstance(context, dict) else {}
    response = payload.get("response")
    response = response if isinstance(response, dict) else {}
    cycle = response.get("cycle")
    cycle = cycle if isinstance(cycle, dict) else {}
    summary = cycle.get("summary")
    summary = summary if isinstance(summary, dict) else {}

    lines = ["n8n workflow 실패"]
    workflow_id = _compact_text(context.get("workflowId")) or "unknown"
    execution_id = _compact_text(context.get("executionId")) or "unknown"
    stage = _compact_text(context.get("stage")) or "unknown"
    lines.append(f"workflow: {workflow_id} / execution: {execution_id}")
    scope = " · ".join(
        value
        for value in (
            _compact_text(context.get("portfolioId")),
            _compact_text(context.get("interval")),
        )
        if value
    )
    lines.append(f"단계: {stage}" + (f" ({scope})" if scope else ""))

    details: dict[str, object] = {"stage": stage}
    exit_code = response.get("exitCode")
    if isinstance(exit_code, int):
        lines.append(f"cycle 종료 코드: {exit_code}")
        details["exitCode"] = exit_code
    for key in ("statusCode", "httpCode", "executionId"):
        value = response.get(key)
        if isinstance(value, (str, int, float, bool)):
            details[f"upstream{key[0].upper()}{key[1:]}"] = value

    if summary:
        symbols = _non_negative_int(summary.get("symbols"))
        failed = _non_negative_int(summary.get("failed"))
        if failed:
            lines.append(f"종목 처리 실패: {failed}/{symbols or '?'}")
        details["summary"] = {
            key: _non_negative_int(summary.get(key))
            for key in ("symbols", "signals", "fills", "skipped", "failed")
            if key in summary
        }

    errors = _workflow_failure_errors(response, cycle)
    if errors:
        lines.extend(f"{symbol} 오류: {error}" for symbol, error in errors)
        details["errors"] = [
            {"symbol": symbol, "error": error} for symbol, error in errors
        ]
    elif error := _compact_text(cycle.get("error") or response.get("error"), 300):
        lines.append(f"원인: {error}")
        details["error"] = error
    else:
        lines.append("상세 원인: n8n execution 및 automation_run_logs 확인")

    severity = "warning" if exit_code == 3 else "critical"
    return {
        "message": "\n".join(lines),
        "details": details,
        "severity": severity,
    }


def _workflow_failure_errors(
    response: dict[str, Any], cycle: dict[str, Any]
) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    items = cycle.get("items")
    items = items if isinstance(items, list) else response.get("items")
    if not isinstance(items, list):
        return values
    for item in items:
        if not isinstance(item, dict):
            continue
        error = _compact_text(item.get("error"), 300)
        if error:
            values.append((_compact_text(item.get("symbol")) or "unknown", error))
        if len(values) == 5:
            break
    return values


def _compact_text(value: object, limit: int = 120) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).replace("\n", " ").strip()
    return text[:limit] if text else None


def _collect_decision_ids(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"decisionId", "riskDecisionId"} and isinstance(child, str):
                found.append(child)
            elif key not in {"analysis", "opinion"}:
                found.extend(_collect_decision_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_decision_ids(child))
    return found


def _token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _hermes_analysis_from_response(payload: object) -> HermesAnalysis:
    if not isinstance(payload, dict):
        raise TypeError("Hermes API response must be JSON")
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
        "skipped": _non_negative_int(summary.get("skipped")),
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


def _grouped_number(value: Any) -> str:
    number = _decimal(value)
    if number is None:
        return "?"
    if number == number.to_integral_value():
        return format(number, ",.0f")
    return format(number, ",f").rstrip("0").rstrip(".")


def _higher_severity(current: str, candidate: str) -> str:
    rank = {"info": 0, "warning": 1, "critical": 2}
    return candidate if rank[candidate] > rank[current] else current


def _safe_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:500] or type(error).__name__


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
