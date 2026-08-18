from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from .models import Candle
from .setup_screening import (
    SetupContext,
    SetupDecision,
    SetupType,
    SlippageAssumption,
    evaluate_setup,
    position_size_reference,
)

GAP_UP_THRESHOLD = Decimal("0.03")
ADVERSE_SLIPPAGE = SlippageAssumption(
    entry_rate=Decimal("0.0005"),
    exit_rate=Decimal("0.0005"),
)
ATR_PERIOD = 14


@dataclass(frozen=True, slots=True)
class DailySetupCandidate:
    symbol: str
    signal_session: date
    close_price: Decimal
    setup_low: Decimal
    ma50: Decimal
    atr14: Decimal
    decision: SetupDecision


@dataclass(frozen=True, slots=True)
class ArmedTradePlan:
    symbol: str
    quantity: Decimal
    execution_open: Decimal
    entry_price: Decimal
    stop_price: Decimal
    planned_heat: Decimal
    setups: tuple[SetupType, ...]


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    armed: bool
    reason: str
    plan: ArmedTradePlan | None


def build_daily_candidate(
    candles: list[Candle], *, context: SetupContext
) -> DailySetupCandidate:
    decision = evaluate_setup(candles, context=context)
    setup_bar = candles[-1]
    return DailySetupCandidate(
        symbol=setup_bar.symbol,
        signal_session=context.signal_session,
        close_price=setup_bar.close_price,
        setup_low=setup_bar.low_price,
        ma50=decision.ma50,
        atr14=wilder_atr(candles, ATR_PERIOD),
        decision=decision,
    )


def arm_candidate(
    candidate: DailySetupCandidate,
    *,
    first_one_minute_open: Decimal,
    equity: Decimal,
    available_cash: Decimal,
    current_open_heat: Decimal = Decimal(0),
    current_cluster_heat: Decimal = Decimal(0),
) -> CandidateDecision:
    if first_one_minute_open <= 0:
        raise ValueError("first one-minute open must be positive")
    if not candidate.decision.approved:
        reasons = (
            *(f"missing:{item}" for item in candidate.decision.missing_checks),
            *(f"violation:{item}" for item in candidate.decision.violations),
        )
        return CandidateDecision(
            armed=False,
            reason=";".join(reasons) or "setup-v2:rejected",
            plan=None,
        )
    gap = first_one_minute_open / candidate.close_price - Decimal(1)
    if gap >= GAP_UP_THRESHOLD:
        return CandidateDecision(
            armed=False,
            reason="violation:gap-up-chase",
            plan=None,
        )
    if not Decimal(0) < candidate.setup_low < first_one_minute_open:
        return CandidateDecision(
            armed=False,
            reason="violation:invalid-stop",
            plan=None,
        )
    sizing = position_size_reference(
        symbol=candidate.symbol,
        equity=equity,
        reference_price=first_one_minute_open,
        stop_price=candidate.setup_low,
        atr=candidate.atr14,
        available_cash=available_cash,
        current_open_heat=current_open_heat,
        current_cluster_heat=current_cluster_heat,
        slippage=ADVERSE_SLIPPAGE,
    )
    if sizing.quantity <= 0:
        return CandidateDecision(
            armed=False,
            reason="violation:below-one-lot",
            plan=None,
        )
    entry_price = first_one_minute_open * (Decimal(1) + ADVERSE_SLIPPAGE.entry_rate)
    stop_price = first_one_minute_open - sizing.effective_stop_distance
    return CandidateDecision(
        armed=True,
        reason="armed",
        plan=ArmedTradePlan(
            symbol=candidate.symbol,
            quantity=sizing.quantity,
            execution_open=first_one_minute_open,
            entry_price=entry_price,
            stop_price=stop_price,
            planned_heat=sizing.planned_heat,
            setups=candidate.decision.setups,
        ),
    )


def stop_touched(*, bar_low: Decimal, stop_price: Decimal) -> bool:
    if min(bar_low, stop_price) <= 0:
        raise ValueError("bar_low and stop_price must be positive")
    return bar_low <= stop_price


def pullback_invalidated(
    candidate: DailySetupCandidate, *, close_price: Decimal
) -> bool:
    if close_price <= 0:
        raise ValueError("close_price must be positive")
    if SetupType.PULLBACK not in candidate.decision.setups:
        return False
    return close_price < candidate.ma50


def wilder_atr(candles: list[Candle], period: int = ATR_PERIOD) -> Decimal:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    if len(candles) < period + 1:
        raise ValueError(f"Wilder ATR{period} needs {period + 1} candles")
    ranges = []
    for previous, current in pairwise(candles):
        ranges.append(
            max(
                current.high_price - current.low_price,
                abs(current.high_price - previous.close_price),
                abs(current.low_price - previous.close_price),
            )
        )
    if len(ranges) < period:
        raise ValueError(f"Wilder ATR{period} needs {period} true ranges")
    average = sum(ranges[:period], start=Decimal(0)) / Decimal(period)
    for true_range in ranges[period:]:
        average = (average * (period - 1) + true_range) / Decimal(period)
    return average
