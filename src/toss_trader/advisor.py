from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .automation import HermesAnalysis, HermesAnalyzer
from .execution import TradeAdvice
from .models import TradeSignal
from .paper import PaperLedgerStore
from .risk import RiskContext

HERMES_TRADE_PROMPT = (
    "너는 paper trading 비교 실험의 보수적 기술적 분석 검토자다. "
    "제공된 JSON만 보고 신호를 승인 또는 거부하라. 도구를 호출하지 마라. "
    "marketContext가 있으면 호가·현재가·최근 체결·상하한·유의사항·수급을 참고하라. "
    "RiskManager가 최종 결정하며 너는 안전 한도를 완화할 수 없다. "
    'JSON 한 개만 응답하라: {"approved": true 또는 false, '
    '"rationale": "한국어 1~3문장"}. 직접적인 투자 권유와 수익 보장은 금지한다.'
)


class HermesTradeAdvisor:
    def __init__(self, *, analyzer: HermesAnalyzer, audit: PaperLedgerStore) -> None:
        self._analyzer = analyzer
        self._audit = audit

    def advise(self, signal: TradeSignal, context: RiskContext) -> TradeAdvice:
        started_at = datetime.now(UTC)
        usage = HermesAnalysis(content="")
        payload = {
            "signal": {
                "id": signal.signal_id,
                "symbol": signal.symbol,
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
        if context.market_context is not None:
            payload["marketContext"] = context.market_context
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


def create_hermes_trade_advisor(
    *, api_key: str, base_url: str, audit: PaperLedgerStore
) -> HermesTradeAdvisor:
    return HermesTradeAdvisor(
        analyzer=HermesAnalyzer(
            api_key=api_key,
            base_url=base_url,
            system_prompt=HERMES_TRADE_PROMPT,
        ),
        audit=audit,
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
