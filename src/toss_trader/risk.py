from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .models import Side, TradeSignal


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal(300000)
    max_position_notional: Decimal = Decimal(1000000)
    max_daily_buy_count: int = 5
    max_open_positions: int = 5
    daily_loss_limit: Decimal = Decimal("-0.03")
    max_consecutive_api_errors: int = 5
    block_new_buys_before_close: timedelta = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    market_close_at: datetime | None = None
    market_is_business_day: bool = True
    position_notional: Decimal = Decimal(0)
    position_quantity: Decimal = Decimal(0)
    available_cash: Decimal | None = None
    daily_buy_count: int = 0
    open_position_count: int = 0
    daily_return_rate: Decimal = Decimal(0)
    consecutive_api_errors: int = 0
    seen_signal_ids: frozenset[str] = field(default_factory=frozenset)
    new_buys_allowed: bool = True


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UniverseCandidateRisk:
    symbol: str
    reference_price: Decimal
    security_type: str
    is_common_share: bool
    status: str
    trading_suspended: bool


@dataclass(frozen=True, slots=True)
class UniverseRiskContext:
    quantity: Decimal
    available_cash: Decimal
    daily_return_rate: Decimal
    consecutive_api_errors: int


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def evaluate(self, signal: TradeSignal, context: RiskContext) -> RiskDecision:
        violations: list[str] = []

        if signal.signal_id in context.seen_signal_ids:
            violations.append("duplicate-signal")
        if signal.side is Side.BUY and not context.new_buys_allowed:
            violations.append("universe-refresh-failed")
        if signal.notional > self._limits.max_order_notional:
            violations.append("max-order-notional")
        if (
            signal.side is Side.BUY
            and context.available_cash is not None
            and signal.notional > context.available_cash
        ):
            violations.append("insufficient-paper-cash")
        if (
            signal.side is Side.BUY
            and context.position_notional + signal.notional
            > self._limits.max_position_notional
        ):
            violations.append("max-position-notional")
        if signal.side is Side.SELL and signal.quantity > context.position_quantity:
            violations.append("insufficient-position")
        if (
            signal.side is Side.BUY
            and context.daily_buy_count >= self._limits.max_daily_buy_count
        ):
            violations.append("max-daily-buys")
        if (
            signal.side is Side.BUY
            and context.position_quantity <= 0
            and context.open_position_count >= self._limits.max_open_positions
        ):
            violations.append("max-open-positions")
        if context.daily_return_rate <= self._limits.daily_loss_limit:
            violations.append("daily-loss-limit")
        if context.consecutive_api_errors >= self._limits.max_consecutive_api_errors:
            violations.append("api-error-kill-switch")
        if not context.market_is_business_day:
            violations.append("market-closed")
        if (
            signal.side is Side.BUY
            and context.market_close_at is not None
            and context.now
            >= context.market_close_at - self._limits.block_new_buys_before_close
        ):
            violations.append("market-close-window")

        return RiskDecision(approved=not violations, violations=tuple(violations))

    def evaluate_universe_candidate(
        self, candidate: UniverseCandidateRisk, context: UniverseRiskContext
    ) -> RiskDecision:
        violations: list[str] = []
        if candidate.security_type != "STOCK":
            violations.append("unsupported-security-type")
        if not candidate.is_common_share:
            violations.append("not-common-share")
        if candidate.status != "ACTIVE":
            violations.append("stock-not-active")
        if candidate.trading_suspended:
            violations.append("trading-suspended")
        if candidate.reference_price <= 0:
            violations.append("invalid-reference-price")
        notional = candidate.reference_price * context.quantity
        if notional > self._limits.max_order_notional:
            violations.append("max-order-notional")
        if notional > context.available_cash:
            violations.append("insufficient-paper-cash")
        if context.daily_return_rate <= self._limits.daily_loss_limit:
            violations.append("daily-loss-limit")
        if context.consecutive_api_errors >= self._limits.max_consecutive_api_errors:
            violations.append("api-error-kill-switch")
        return RiskDecision(approved=not violations, violations=tuple(violations))
