from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import PaperFill, TradeSignal
from .paper import PaperLedgerStore
from .risk import RiskContext, RiskDecision, RiskManager


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    decision: RiskDecision
    fill: PaperFill | None
    decision_id: str


class PaperTradingService:
    def __init__(
        self,
        *,
        ledger: PaperLedgerStore,
        risk_manager: RiskManager,
        initial_cash: Decimal = Decimal(1000000),
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        self._ledger = ledger
        self._risk_manager = risk_manager
        self._initial_cash = initial_cash

    def submit(
        self,
        signal: TradeSignal,
        *,
        now: datetime,
        market_close_at: datetime | None = None,
        market_is_business_day: bool = True,
        daily_return_rate: Decimal = Decimal(0),
        consecutive_api_errors: int = 0,
    ) -> PaperExecutionResult:
        context = RiskContext(
            now=now,
            market_close_at=market_close_at,
            market_is_business_day=market_is_business_day,
            position_notional=self._ledger.position_notional(
                signal.symbol, mark_price=signal.reference_price
            ),
            position_quantity=self._ledger.position_quantity(signal.symbol),
            available_cash=self._ledger.cash_balance(self._initial_cash),
            daily_buy_count=self._ledger.daily_buy_count(now.date()),
            daily_return_rate=daily_return_rate,
            consecutive_api_errors=consecutive_api_errors,
            seen_signal_ids=self._ledger.seen_signal_ids(),
        )
        decision = self._risk_manager.evaluate(signal, context)
        decision_id = self._ledger.record_risk_decision(
            signal,
            decision,
            context,
            evaluated_at=now,
        )
        fill = (
            self._ledger.execute(signal, executed_at=now) if decision.approved else None
        )
        return PaperExecutionResult(
            decision=decision,
            fill=fill,
            decision_id=decision_id,
        )
