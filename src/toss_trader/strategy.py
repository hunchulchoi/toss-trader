from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from .models import Side, TradeSignal


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
    if side is None or label is None:
        return None

    strategy_name = f"MA{short_window}/MA{long_window}"
    signal_key = entry_key if label == "trend entry" and entry_key else as_of.isoformat()
    return TradeSignal(
        signal_id=f"ma-{short_window}-{long_window}-{symbol}-{signal_key}",
        symbol=symbol,
        side=side,
        reference_price=closes[-1],
        quantity=quantity,
        reason=f"{strategy_name} {label}",
    )


def _average(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=Decimal(0)) / Decimal(len(values))
