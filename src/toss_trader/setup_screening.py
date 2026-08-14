from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from .models import Candle


class SetupType(StrEnum):
    PULLBACK = "pullback"
    OVERSOLD_REVERSAL = "oversold-reversal"
    FLOW_REVERSAL = "flow-reversal"


class ValuationTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"

    @property
    def multiplier(self) -> Decimal:
        return {
            ValuationTier.A: Decimal("1.5"),
            ValuationTier.B: Decimal("1.0"),
            ValuationTier.C: Decimal("0.6"),
        }[self]


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    foreign_previous: Decimal
    foreign_current: Decimal
    institutional_current: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class ValuationEvidence:
    forward_per_growth: Decimal | None = None
    sector_per_percentile: Decimal | None = None
    sector_relative_overvalued: bool = False
    pure_technical_rebound: bool = False


@dataclass(frozen=True, slots=True)
class SetupContext:
    volatile_market: bool = False
    flow: FlowSnapshot | None = None
    valuation: ValuationEvidence | None = None
    stop_price: Decimal | None = None
    averaging_down: bool = False
    event_imminent: bool | None = None
    gap_up_chase: bool | None = None


@dataclass(frozen=True, slots=True)
class SetupDecision:
    symbol: str
    approved: bool
    setups: tuple[SetupType, ...]
    violations: tuple[str, ...]
    missing_checks: tuple[str, ...]
    rsi14: Decimal
    ma50: Decimal
    ma200: Decimal
    ma50_distance: Decimal
    flow_stars: int
    valuation_tier: ValuationTier
    confidence_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class PositionSizeReference:
    risk_budget: Decimal
    stop_loss_rate: Decimal
    uncapped_notional: Decimal
    executable_notional: Decimal
    capped: bool


def evaluate_setup(
    candles: list[Candle], *, context: SetupContext
) -> SetupDecision:
    _validate_candles(candles)
    latest = candles[-200:]
    closes = [item.close_price for item in latest]
    current = closes[-1]
    previous = latest[-2]
    candle = latest[-1]
    ma50 = _average(closes[-50:])
    ma200 = _average(closes)
    distance = current / ma50 - Decimal(1)
    rsi14 = _rsi(closes, 14)

    setups: list[SetupType] = []
    if current > ma50 > ma200 and Decimal(0) <= distance <= Decimal("0.04"):
        setups.append(SetupType.PULLBACK)
    if rsi14 <= 35 and candle.close_price > candle.open_price and current > previous.high_price:
        setups.append(SetupType.OVERSOLD_REVERSAL)

    flow_stars = 0
    if (
        context.flow is not None
        and context.flow.foreign_previous < 0 < context.flow.foreign_current
    ):
        setups.append(SetupType.FLOW_REVERSAL)
        flow_stars = 2 if context.flow.institutional_current > 0 else 1

    violations: list[str] = []
    required_setups = 2 if context.volatile_market else 1
    if len(setups) < required_setups:
        violations.append("insufficient-setups")
    if rsi14 >= 70:
        violations.append("rsi-chase")
    if current / previous.close_price - Decimal(1) <= Decimal("-0.03"):
        violations.append("falling-knife")
    if (
        context.stop_price is not None
        and current <= context.stop_price * Decimal("1.05")
    ):
        violations.append("stop-line-proximity")
    if context.averaging_down:
        violations.append("averaging-down")
    if context.event_imminent is True:
        violations.append("event-imminent")
    if context.gap_up_chase is True:
        violations.append("gap-up-chase")

    missing_checks: list[str] = []
    if context.event_imminent is None:
        missing_checks.append("event-calendar")
    if context.gap_up_chase is None:
        missing_checks.append("gap-up-review")

    tier = valuation_tier(context.valuation)
    return SetupDecision(
        symbol=candle.symbol,
        approved=not violations and not missing_checks,
        setups=tuple(setups),
        violations=tuple(violations),
        missing_checks=tuple(missing_checks),
        rsi14=_rounded(rsi14),
        ma50=_rounded(ma50),
        ma200=_rounded(ma200),
        ma50_distance=_rounded(distance),
        flow_stars=flow_stars,
        valuation_tier=tier,
        confidence_multiplier=tier.multiplier,
    )


def valuation_tier(evidence: ValuationEvidence | None) -> ValuationTier:
    if evidence is None:
        return ValuationTier.B
    if evidence.sector_relative_overvalued or evidence.pure_technical_rebound:
        return ValuationTier.C
    if (
        evidence.forward_per_growth is not None
        and evidence.forward_per_growth >= Decimal("0.25")
        and evidence.sector_per_percentile is not None
        and evidence.sector_per_percentile <= Decimal("0.30")
    ):
        return ValuationTier.A
    return ValuationTier.B


def position_size_reference(
    *,
    stop_loss_rate: Decimal,
    max_order_notional: Decimal,
    available_cash: Decimal,
    risk_budget: Decimal = Decimal(400000),
) -> PositionSizeReference:
    if not Decimal(0) < stop_loss_rate < Decimal(1):
        raise ValueError("stop_loss_rate must satisfy 0 < rate < 1")
    if risk_budget <= 0:
        raise ValueError("risk_budget must be positive")
    if max_order_notional <= 0:
        raise ValueError("max_order_notional must be positive")
    if available_cash < 0:
        raise ValueError("available_cash must not be negative")
    uncapped = risk_budget / stop_loss_rate
    executable = min(uncapped, max_order_notional, available_cash)
    return PositionSizeReference(
        risk_budget=risk_budget,
        stop_loss_rate=stop_loss_rate,
        uncapped_notional=uncapped,
        executable_notional=executable,
        capped=executable < uncapped,
    )


def _validate_candles(candles: list[Candle]) -> None:
    if len(candles) < 200:
        raise ValueError(f"need 200 daily candles, found {len(candles)}")
    latest = candles[-200:]
    if any(item.interval != "1d" for item in latest):
        raise ValueError("setup screening requires daily candles")
    if len({item.symbol for item in latest}) != 1:
        raise ValueError("setup screening candles must share one symbol")


def _rsi(closes: list[Decimal], period: int) -> Decimal:
    changes = [current - previous for previous, current in pairwise(closes)]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    average_gain = _average(gains[:period])
    average_loss = _average(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_gain == 0 and average_loss == 0:
        return Decimal(50)
    if average_loss == 0:
        return Decimal(100)
    relative_strength = average_gain / average_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, start=Decimal(0)) / Decimal(len(values))


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))
