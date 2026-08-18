from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .models import PaperFill, Side, TradeSignal, V2PositionPlan
from .paper import PaperLedgerStore
from .risk import RiskContext, RiskDecision, RiskLimits, RiskManager


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
        self._preflight_risk_manager = RiskManager(RiskLimits())
        self._initial_cash = initial_cash
        self._advisor = advisor

    def has_position(self, symbol: str) -> bool:
        return self._ledger.position_quantity(symbol) > 0

    def position_quantity(self, symbol: str) -> Decimal:
        return self._ledger.position_quantity(symbol)

    def available_cash(self) -> Decimal:
        return self._ledger.cash_balance(self._initial_cash)

    def v2_position_plan(self, symbol: str) -> V2PositionPlan | None:
        return self._ledger.v2_position_plan(symbol)

    def v2_position_plans(self) -> dict[str, V2PositionPlan]:
        return self._ledger.v2_position_plans()

    def unplanned_position_symbols(self) -> tuple[str, ...]:
        held = {
            symbol
            for symbol, quantity in self._ledger.position_quantities().items()
            if quantity > 0
        }
        return tuple(sorted(held - self.v2_position_plans().keys()))

    def open_v2_heat(self) -> Decimal:
        return sum(
            (plan.planned_heat for plan in self.v2_position_plans().values()),
            start=Decimal(0),
        )

    def cluster_v2_heat(self, cluster_id: str) -> Decimal:
        if not cluster_id.strip():
            raise ValueError("cluster_id must not be empty")
        return sum(
            (
                plan.planned_heat
                for plan in self.v2_position_plans().values()
                if plan.cluster_id == cluster_id
            ),
            start=Decimal(0),
        )

    def store_v2_position_plan(self, plan: V2PositionPlan) -> None:
        self._ledger.upsert_v2_position_plan(plan)

    def mark_v2_exit_pending(
        self, symbol: str, *, reason: str, triggered_at: datetime
    ) -> None:
        self._ledger.mark_v2_exit_pending(
            symbol, reason=reason, triggered_at=triggered_at
        )

    def clear_v2_position_plan(self, symbol: str) -> None:
        self._ledger.delete_v2_position_plan(symbol)

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
        costs = self._ledger.estimate_costs(signal)
        cash = self._ledger.cash_balance(self._initial_cash)
        context = RiskContext(
            now=now,
            market_close_at=market_close_at,
            market_is_business_day=market_is_business_day,
            position_notional=self._ledger.position_notional(
                signal.symbol, mark_price=signal.reference_price
            ),
            position_quantity=self._ledger.position_quantity(signal.symbol),
            available_cash=(
                cash - costs.total if signal.side is Side.BUY else cash
            ),
            daily_buy_count=self._ledger.daily_buy_count(now.date()),
            open_position_count=len(self._ledger.position_quantities()),
            daily_return_rate=daily_return_rate,
            consecutive_api_errors=consecutive_api_errors,
            seen_signal_ids=self._ledger.seen_signal_ids(),
            new_buys_allowed=new_buys_allowed,
        )
        decision: RiskDecision | None = None
        if self._advisor is not None:
            preflight = self._preflight_risk_manager.evaluate(signal, context)
            if not preflight.approved:
                decision = preflight
        if self._advisor is not None and decision is None:
            try:
                advice = self._advisor.advise(signal, context)
                context = replace(
                    context,
                    advisor_status="approved" if advice.approved else "rejected",
                    advisor_rationale=advice.rationale,
                )
            except Exception:  # noqa: BLE001
                context = replace(context, advisor_status="unavailable")
        if decision is None:
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
