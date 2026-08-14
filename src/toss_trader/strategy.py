from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import Side, TradeSignal


@dataclass(frozen=True, slots=True)
class MaCrossoverEvaluation:
    signal: TradeSignal | None
    close: Decimal
    short_ma: Decimal
    long_ma: Decimal
    relation: str


def evaluate_ma_crossover(
    *,
    symbol: str,
    closes: Sequence[Decimal],
    as_of: datetime,
    quantity: Decimal,
    short_window: int = 20,
    long_window: int = 60,
    allow_trend_entry: bool = False,
    entry_key: str | None = None,
) -> MaCrossoverEvaluation:
    if short_window <= 0 or long_window <= 0 or short_window >= long_window:
        raise ValueError("windows must satisfy 0 < short_window < long_window")
    if len(closes) < long_window + 1:
        raise ValueError("one extra close beyond long_window is required")
    if any(close <= 0 for close in closes):
        raise ValueError("close prices must be positive")

    previous = closes[:-1]
    previous_short = _average(previous[-short_window:])
    previous_long = _average(previous[-long_window:])
    current_short = _average(closes[-short_window:])
    current_long = _average(closes[-long_window:])
    if current_short > current_long:
        relation = "above"
    elif current_short < current_long:
        relation = "below"
    else:
        relation = "equal"

    side: Side | None = None
    label: str | None = None
    if previous_short <= previous_long and current_short > current_long:
        side = Side.BUY
        label = "crossed above"
    elif previous_short >= previous_long and current_short < current_long:
        side = Side.SELL
        label = "crossed below"
    elif allow_trend_entry and current_short > current_long:
        side = Side.BUY
        label = "trend entry"
    signal = None
    if side is not None and label is not None:
        strategy_name = f"MA{short_window}/MA{long_window}"
        signal_key = (
            entry_key if label == "trend entry" and entry_key else as_of.isoformat()
        )
        signal = TradeSignal(
            signal_id=f"ma-{short_window}-{long_window}-{symbol}-{signal_key}",
            symbol=symbol,
            side=side,
            reference_price=closes[-1],
            quantity=quantity,
            reason=f"{strategy_name} {label}",
        )
    return MaCrossoverEvaluation(
        signal=signal,
        close=closes[-1],
        short_ma=current_short,
        long_ma=current_long,
        relation=relation,
    )


def ma_trend_continuation_signal(
    *,
    evaluation: MaCrossoverEvaluation,
    symbol: str,
    short_window: int,
    long_window: int,
    quantity: Decimal,
    entry_key: str,
) -> TradeSignal | None:
    if evaluation.signal is not None:
        return None
    if not (evaluation.close > evaluation.short_ma > evaluation.long_ma):
        return None
    return TradeSignal(
        signal_id=f"ma-{short_window}-{long_window}-{symbol}-cont-{entry_key}",
        symbol=symbol,
        side=Side.BUY,
        reference_price=evaluation.close,
        quantity=quantity,
        reason=f"MA{short_window}/MA{long_window} trend continuation",
    )


def ma_crossover_signal(
    *,
    symbol: str,
    closes: Sequence[Decimal],
    as_of: datetime,
    quantity: Decimal,
    short_window: int = 20,
    long_window: int = 60,
    allow_trend_entry: bool = False,
    entry_key: str | None = None,
) -> TradeSignal | None:
    return evaluate_ma_crossover(
        symbol=symbol,
        closes=closes,
        as_of=as_of,
        quantity=quantity,
        short_window=short_window,
        long_window=long_window,
        allow_trend_entry=allow_trend_entry,
        entry_key=entry_key,
    ).signal


def _average(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=Decimal(0)) / Decimal(len(values))
