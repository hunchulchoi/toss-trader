from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
    "필수 데이터 결손·임박 이벤트·갭·수량·현금·시간·Risk 차단은 이미 하드 "
    "게이트이므로 우회할 수 없다. "
    "한도 숫자만으로 승인하지 마라. "
    'JSON 한 개만 응답하라: {"approved": true 또는 false, '
    '"rationale": "한국어 1~3문장"}. 직접적인 투자 권유와 수익 보장은 금지한다.'
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
    return TradeAdvice(approved=value["approved"], rationale=rationale.strip()[:1000])
