from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .automation import HermesAnalysis, HermesAnalyzer
from .execution import TradeAdvice
from .models import Candle, TradeSignal
from .paper import PaperLedgerStore
from .risk import RiskContext
from .v2_engine import ArmedTradePlan, DailySetupCandidate

HERMES_TRADE_PROMPT = (
    "너는 독립 Hermes paper trading 실험의 보수적 기술적 분석 검토자다. "
    "제공된 JSON만 보고 신호를 승인 또는 거부하라. 도구를 호출하지 마라. "
    "RiskManager가 최종 결정하며 너는 안전 한도를 완화할 수 없다. "
    "setup-v2의 missing-price-setup, rsi-chase, falling-knife는 참고 근거이며 "
    "그 위반만으로 자동 거부하지 마라. 일봉·분봉·수급을 함께 판단하라. "
    "signal.reason이 Hermes Hunter momentum reclaim이면 전일 setup보다 첫 1시간의 "
    "상승·눌림·재돌파 유지력과 현재 손절 거리를 우선 검토하라. 기존 Hunter "
    "approve는 참고일 뿐 최종 승인이 아니다. "
    "필수 데이터 결손·임박 이벤트·갭·수량·현금·시간·Risk 차단은 이미 하드 "
    "게이트이므로 우회할 수 없다. "
    "한도 숫자만으로 승인하지 마라. "
    "거부할 경우 허용 vetoCodes는 LIQUIDITY_TOO_THIN, RECLAIM_LOST, "
    "MARKET_DIVERGENCE, DATA_MISSING, EVENT_RISK뿐이다. 수치 evidence 없이 "
    "임의 저항선이나 막연한 힘 부족을 거부 근거로 쓰지 마라. "
    'JSON 한 개만 응답하라: {"approved": true 또는 false, '
    '"rationale": "한국어 1~3문장", "vetoCodes": [], "evidence": {}}. '
    "직접적인 투자 권유와 수익 보장은 금지한다."
)

HERMES_MOMENTUM_SHADOW_PROMPT = (
    "너는 장중 눌림 재돌파 Hunter의 비매매 검토자다. 제공된 JSON만 보고 "
    "각 후보를 approve, watch, reject 중 하나로 분류하라. Hunter 신호와 "
    "setup-v2.3 위반은 참고 근거일 뿐이며 주문을 만들거나 수익을 보장하지 마라. "
    "시장 동조, 상승 후 눌림의 질, 재돌파 유지력, 거래대금 가속, 손절 거리를 "
    "함께 검토하라. 도구를 호출하지 마라. JSON 한 개만 응답하라: "
    '{"decisions":[{"symbol":"종목코드","verdict":"approve|watch|reject",'
    '"rationale":"한국어 1~2문장"}]}'
)


class HermesTradeAdvisor:
    def __init__(
        self,
        *,
        analyzer: HermesAnalyzer,
        audit: PaperLedgerStore,
        symbol_names: Mapping[str, str],
    ) -> None:
        self._analyzer = analyzer
        self._audit = audit
        self._symbol_names = dict(symbol_names)

    def advise(
        self,
        signal: TradeSignal,
        context: RiskContext,
        review: Mapping[str, object] | None = None,
    ) -> TradeAdvice:
        name = self._symbol_names.get(signal.symbol, "").strip()
        if not name:
            raise RuntimeError(f"company name missing for Hermes symbol: {signal.symbol}")
        started_at = datetime.now(UTC)
        usage = HermesAnalysis(content="")
        payload = {
            "signal": {
                "id": signal.signal_id,
                "symbol": signal.symbol,
                "name": name,
                "side": signal.side.value,
                "quantity": str(signal.quantity),
                "referencePrice": str(signal.reference_price),
                "notional": str(signal.notional),
                "reason": signal.reason,
            },
            "riskContext": {
                "marketIsBusinessDay": context.market_is_business_day,
                "marketCloseAt": (
                    context.market_close_at.isoformat()
                    if context.market_close_at is not None
                    else None
                ),
                "positionNotional": str(context.position_notional),
                "positionQuantity": str(context.position_quantity),
                "availableCash": (
                    str(context.available_cash)
                    if context.available_cash is not None
                    else None
                ),
                "dailyBuyCount": context.daily_buy_count,
                "dailyReturnRate": str(context.daily_return_rate),
                "consecutiveApiErrors": context.consecutive_api_errors,
                "newBuysAllowed": context.new_buys_allowed,
            },
        }
        if review:
            payload["market"] = dict(review)
        try:
            usage = self._analyzer.analyze(payload)
            advice = _parse_advice(usage.content)
            self._record(
                status="succeeded",
                started_at=started_at,
                usage=usage,
                details={
                    "portfolioId": "hermes",
                    "signalId": signal.signal_id,
                    "symbol": signal.symbol,
                    "side": signal.side.value,
                    "approved": advice.approved,
                    "rationale": advice.rationale,
                    "vetoCodes": list(advice.veto_codes),
                    "evidence": dict(advice.evidence or {}),
                },
            )
            return advice
        except Exception as error:
            self._record(
                status="failed",
                started_at=started_at,
                usage=usage,
                error=str(error),
                details={
                    "portfolioId": "hermes",
                    "signalId": signal.signal_id,
                    "symbol": signal.symbol,
                    "side": signal.side.value,
                },
            )
            raise

    def _record(
        self,
        *,
        status: str,
        started_at: datetime,
        usage: HermesAnalysis,
        details: dict[str, object],
        error: str | None = None,
    ) -> None:
        self._audit.record_automation_run(
            run_type="hermes_trade",
            status=status,
            stage="decision" if status == "succeeded" else "hermes",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            error=error,
            details=details,
        )


def hermes_market_review(
    *,
    daily: Sequence[Candle],
    minutes: Sequence[Candle],
    candidate: DailySetupCandidate | None = None,
    plan: ArmedTradePlan | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "daily": _compact_bars(daily, 30),
        "minutes": _compact_bars(minutes, 60),
    }
    if candidate is not None:
        decision = candidate.decision
        flow = decision.flow_summary
        payload["setup"] = {
            "signalSession": candidate.signal_session.isoformat(),
            "close": str(candidate.close_price),
            "setupLow": str(candidate.setup_low),
            "atr14": str(candidate.atr14),
            "approved": decision.approved,
            "setups": [str(item) for item in decision.setups],
            "violations": list(decision.violations),
            "missingChecks": list(decision.missing_checks),
            "rsi14": str(decision.rsi14),
            "ma50": str(decision.ma50),
            "ma200": str(decision.ma200),
            "ma50Distance": str(decision.ma50_distance),
            "flowStars": decision.flow_stars,
            "valuationTier": str(decision.valuation_tier),
            "flow": None
            if flow is None
            else {
                "latestSession": flow.latest_session.isoformat(),
                "previous5dRatio": str(flow.previous_5d_ratio),
                "current5dRatio": str(flow.current_5d_ratio),
                "institutional5dRatio": str(flow.institutional_5d_ratio),
                "foreignReversal": flow.foreign_reversal,
                "institutionalConfirmed": flow.institutional_confirmed,
            },
        }
    if plan is not None:
        payload["plan"] = {
            "quantity": str(plan.quantity),
            "executionOpen": str(plan.execution_open),
            "entryPrice": str(plan.entry_price),
            "stopPrice": str(plan.stop_price),
            "plannedHeat": str(plan.planned_heat),
            "setups": [str(item) for item in plan.setups],
            "setupSession": plan.setup_session.isoformat(),
        }
        recent = tuple(minutes[-5:])
        previous = tuple(minutes[-10:-5])
        if recent:
            recent_average = sum(
                (bar.close_price * bar.volume for bar in recent), Decimal(0)
            ) / len(recent)
            previous_average = (
                sum(
                    (bar.close_price * bar.volume for bar in previous),
                    Decimal(0),
                )
                / len(previous)
                if previous
                else Decimal(0)
            )
            order_notional = plan.entry_price * plan.quantity
            payload["liquidity"] = {
                "sampleBars": len(recent),
                "recent5mAverageTradingValue": str(recent_average),
                "previous5mAverageTradingValue": str(previous_average),
                "tradingValueAcceleration": (
                    str(recent_average / previous_average)
                    if previous_average > 0
                    else None
                ),
                "orderNotional": str(order_notional),
                "orderParticipationRate": (
                    str(order_notional / recent_average)
                    if recent_average > 0
                    else None
                ),
            }
    return payload


def _compact_bars(candles: Sequence[Candle], limit: int) -> list[dict[str, str]]:
    return [
        {
            "t": candle.timestamp.isoformat(),
            "o": str(candle.open_price),
            "h": str(candle.high_price),
            "l": str(candle.low_price),
            "c": str(candle.close_price),
            "v": str(candle.volume),
        }
        for candle in candles[-limit:]
    ]


def create_hermes_trade_advisor(
    *,
    api_key: str,
    base_url: str,
    audit: PaperLedgerStore,
    symbol_names: Mapping[str, str],
) -> HermesTradeAdvisor:
    return HermesTradeAdvisor(
        analyzer=HermesAnalyzer(
            api_key=api_key,
            base_url=base_url,
            system_prompt=HERMES_TRADE_PROMPT,
        ),
        audit=audit,
        symbol_names=symbol_names,
    )


def review_momentum_shadow_once(
    *,
    api_key: str,
    base_url: str,
    audit: PaperLedgerStore,
    payload: Mapping[str, Any],
    symbol_names: Mapping[str, str],
) -> dict[str, Any]:
    session_date = str(payload.get("sessionDate") or "")
    rule_version = str(payload.get("ruleVersion") or "")
    selected = payload.get("selected")
    if not session_date or not rule_version:
        raise ValueError("momentum shadow identity is required")
    if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)):
        raise TypeError("momentum shadow selected candidates must be an array")
    candidates = [dict(item) for item in selected if isinstance(item, Mapping)]
    if not candidates:
        return {"status": "not-requested", "decisions": []}
    for run in audit.recent_automation_runs(
        limit=100, run_type="momentum-shadow-advice"
    ):
        details = run.get("details")
        if (
            run.get("status") == "succeeded"
            and isinstance(details, Mapping)
            and details.get("sessionDate") == session_date
            and details.get("ruleVersion") == rule_version
        ):
            return {**dict(details), "cacheHit": True}
    requested_symbols = [str(item.get("symbol") or "") for item in candidates]
    request_payload = {
        "sessionDate": session_date,
        "ruleVersion": rule_version,
        "strategyInput": False,
        "shadowOnly": True,
        "candidates": [
            {**item, "name": symbol_names.get(str(item.get("symbol") or ""))}
            for item in candidates
        ],
    }
    analyzer = HermesAnalyzer(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=90,
        system_prompt=HERMES_MOMENTUM_SHADOW_PROMPT,
    )
    started_at = datetime.now(UTC)
    usage = HermesAnalysis(content="")
    try:
        usage = analyzer.analyze(request_payload)
        decisions = _parse_momentum_shadow_advice(
            usage.content, expected_symbols=requested_symbols
        )
        details: dict[str, Any] = {
            "sessionDate": session_date,
            "ruleVersion": rule_version,
            "strategyInput": False,
            "shadowOnly": True,
            "decisions": decisions,
        }
        run_id = audit.record_automation_run(
            run_type="momentum-shadow-advice",
            status="succeeded",
            stage="decision",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            details=details,
        )
        return {**details, "auditRunId": run_id, "cacheHit": False}
    except Exception as error:
        audit.record_automation_run(
            run_type="momentum-shadow-advice",
            status="failed",
            stage="hermes",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            error=str(error),
            details={
                "sessionDate": session_date,
                "ruleVersion": rule_version,
                "strategyInput": False,
                "shadowOnly": True,
                "symbols": requested_symbols,
            },
        )
        raise


def _parse_advice(content: str) -> TradeAdvice:
    normalized = content.strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1]).strip()
    try:
        value: Any = json.loads(normalized)
    except json.JSONDecodeError as error:
        raise RuntimeError("Hermes trade response is not JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("approved"), bool):
        raise TypeError("Hermes trade response is missing approved")
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise RuntimeError("Hermes trade response is missing rationale")
    veto_codes = value.get("vetoCodes", [])
    allowed_veto_codes = {
        "LIQUIDITY_TOO_THIN",
        "RECLAIM_LOST",
        "MARKET_DIVERGENCE",
        "DATA_MISSING",
        "EVENT_RISK",
    }
    if not isinstance(veto_codes, list) or not all(
        isinstance(code, str) and code in allowed_veto_codes for code in veto_codes
    ):
        raise ValueError("Hermes trade response contains invalid vetoCodes")
    evidence = value.get("evidence", {})
    if not isinstance(evidence, Mapping):
        raise TypeError("Hermes trade response evidence must be an object")
    return TradeAdvice(
        approved=value["approved"],
        rationale=rationale.strip()[:1000],
        veto_codes=tuple(dict.fromkeys(veto_codes)),
        evidence=dict(evidence),
    )


def _parse_momentum_shadow_advice(
    content: str, *, expected_symbols: Sequence[str]
) -> list[dict[str, str]]:
    normalized = content.strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1]).strip()
    try:
        value: Any = json.loads(normalized)
    except json.JSONDecodeError as error:
        raise RuntimeError("Hermes momentum response is not JSON") from error
    rows = value.get("decisions") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise TypeError("Hermes momentum response is missing decisions")
    expected = list(dict.fromkeys(expected_symbols))
    parsed: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("Hermes momentum decision must be an object")
        symbol = str(row.get("symbol") or "")
        verdict = str(row.get("verdict") or "").lower()
        rationale = str(row.get("rationale") or "").strip()
        if symbol not in expected or symbol in parsed:
            raise ValueError("Hermes momentum decision symbol is invalid")
        if verdict not in {"approve", "watch", "reject"} or not rationale:
            raise ValueError("Hermes momentum decision content is invalid")
        parsed[symbol] = {
            "symbol": symbol,
            "verdict": verdict,
            "rationale": rationale[:1000],
        }
    if set(parsed) != set(expected):
        raise ValueError("Hermes momentum decisions are incomplete")
    return [parsed[symbol] for symbol in expected]
