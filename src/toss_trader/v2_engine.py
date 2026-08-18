from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from .models import Candle
from .setup_screening import (
    SetupContext,
    SetupDecision,
    SetupType,
    SlippageAssumption,
    evaluate_setup,
    position_size_reference,
)

SEOUL = ZoneInfo("Asia/Seoul")
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
    setup_session: date
    ma50: Decimal
    signal_close: Decimal


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
    first_completed_bar: Candle,
    session_open_at: datetime,
    equity: Decimal,
    available_cash: Decimal,
    current_open_heat: Decimal = Decimal(0),
    current_cluster_heat: Decimal = Decimal(0),
) -> CandidateDecision:
    """Arm D+1 entry from the first regular-session 1m bar.

    The caller must pass a completed 1m bar. This function cannot observe
    whether the bar is closed; it only checks interval, timezone, timestamp
    identity, and that the KST session date is after ``signal_session``.
    """
    _require_session_open_bar(
        first_completed_bar,
        session_open_at=session_open_at,
        signal_session=candidate.signal_session,
        symbol=candidate.symbol,
    )
    execution_open = first_completed_bar.open_price
    if not candidate.decision.approved:
        return CandidateDecision(
            armed=False,
            reason=_setup_v2_reason(candidate.decision),
            plan=None,
        )
    gap = execution_open / candidate.close_price - Decimal(1)
    if gap >= GAP_UP_THRESHOLD:
        return CandidateDecision(
            armed=False,
            reason="setup-v2:violation:gap-up-chase",
            plan=None,
        )
    if not Decimal(0) < candidate.setup_low < execution_open:
        return CandidateDecision(
            armed=False,
            reason="setup-v2:violation:invalid-stop",
            plan=None,
        )
    sizing = position_size_reference(
        symbol=candidate.symbol,
        equity=equity,
        reference_price=execution_open,
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
            reason="setup-v2:violation:below-one-lot",
            plan=None,
        )
    entry_price = execution_open * (Decimal(1) + ADVERSE_SLIPPAGE.entry_rate)
    stop_price = execution_open - sizing.effective_stop_distance
    return CandidateDecision(
        armed=True,
        reason="setup-v2:armed",
        plan=ArmedTradePlan(
            symbol=candidate.symbol,
            quantity=sizing.quantity,
            execution_open=execution_open,
            entry_price=entry_price,
            stop_price=stop_price,
            planned_heat=sizing.planned_heat,
            setups=candidate.decision.setups,
            setup_session=candidate.signal_session,
            ma50=candidate.ma50,
            signal_close=candidate.close_price,
        ),
    )


def stop_touched(*, bar_low: Decimal, stop_price: Decimal) -> bool:
    if min(bar_low, stop_price) <= 0:
        raise ValueError("bar_low and stop_price must be positive")
    return bar_low <= stop_price


def pullback_invalidated(plan: ArmedTradePlan, *, close_price: Decimal) -> bool:
    if close_price <= 0:
        raise ValueError("close_price must be positive")
    if SetupType.PULLBACK not in plan.setups:
        return False
    return close_price < plan.ma50


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


def _require_session_open_bar(
    bar: Candle,
    *,
    session_open_at: datetime,
    signal_session: date,
    symbol: str,
) -> None:
    if bar.interval != "1m":
        raise ValueError("first completed bar must be a 1m candle")
    if bar.symbol != symbol:
        raise ValueError("completed bar symbol must match the candidate")
    if session_open_at.tzinfo is None or session_open_at.utcoffset() is None:
        raise ValueError("session_open_at must include a timezone offset")
    if bar.timestamp != session_open_at:
        raise ValueError("completed bar timestamp must equal session_open_at")
    if bar.timestamp.astimezone(SEOUL).date() <= signal_session:
        raise ValueError("session open must be after the setup signal session")


def _setup_v2_reason(decision: SetupDecision) -> str:
    parts = (
        *(f"missing:{item}" for item in decision.missing_checks),
        *(f"violation:{item}" for item in decision.violations),
    )
    if not parts:
        return "setup-v2:rejected"
    return "setup-v2:" + ",".join(parts)
