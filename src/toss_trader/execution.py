from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .models import PaperFill, TradeSignal
from .paper import PaperLedgerStore
from .risk import RiskContext, RiskDecision, RiskManager


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    decision: RiskDecision
    fill: PaperFill | None
    decision_id: str


@dataclass(frozen=True, slots=True)
class TradeAdvice:
    approved: bool
    rationale: str


class TradeAdvisor(Protocol):
    def advise(self, signal: TradeSignal, context: RiskContext) -> TradeAdvice: ...


class PaperTradingService:
    def __init__(
        self,
        *,
        ledger: PaperLedgerStore,
        risk_manager: RiskManager,
        initial_cash: Decimal = Decimal(1000000),
        advisor: TradeAdvisor | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        self._ledger = ledger
        self._risk_manager = risk_manager
        self._initial_cash = initial_cash
        self._advisor = advisor

    def has_position(self, symbol: str) -> bool:
        return self._ledger.position_quantity(symbol) > 0

    def submit(
        self,
        signal: TradeSignal,
        *,
        now: datetime,
        market_close_at: datetime | None = None,
        market_is_business_day: bool = True,
        daily_return_rate: Decimal = Decimal(0),
        consecutive_api_errors: int = 0,
        new_buys_allowed: bool = True,
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
            open_position_count=len(self._ledger.position_quantities()),
            daily_return_rate=daily_return_rate,
            consecutive_api_errors=consecutive_api_errors,
            seen_signal_ids=self._ledger.seen_signal_ids(),
            new_buys_allowed=new_buys_allowed,
        )
        if self._advisor is not None:
            try:
                advice = self._advisor.advise(signal, context)
                context = replace(
                    context,
                    advisor_status="approved" if advice.approved else "rejected",
                )
            except Exception:  # noqa: BLE001
                context = replace(context, advisor_status="unavailable")
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
