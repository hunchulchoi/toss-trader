from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from .backtest import BacktestResult, run_ma_backtest
from .models import Candle


@dataclass(frozen=True, slots=True)
class WalkForwardMetrics:
    total_return_rate: Decimal
    buy_hold_return_rate: Decimal
    excess_return_rate: Decimal
    max_drawdown_rate: Decimal
    completed_trades: int
    win_rate: Decimal
    total_costs: Decimal


@dataclass(frozen=True, slots=True)
class WalkForwardCandidate:
    short_window: int
    long_window: int
    train_rank: int
    validation_rank: int
    overfit_warning: bool
    train: WalkForwardMetrics
    validation: WalkForwardMetrics


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    symbol: str
    interval: str
    train_candle_count: int
    validation_candle_count: int
    train_ratio: Decimal
    selected_short_window: int
    selected_long_window: int
    selected_overfit_warning: bool
    candidates: tuple[WalkForwardCandidate, ...]


def run_ma_walk_forward(
    *,
    candles: Sequence[Candle],
    short_windows: Sequence[int],
    long_windows: Sequence[int],
    train_ratio: Decimal,
    quantity: Decimal,
    initial_cash: Decimal,
    slippage_rate: Decimal = Decimal(0),
) -> WalkForwardResult:
    """Select MA parameters on training data and evaluate a holdout partition."""
    if not Decimal(0) < train_ratio < Decimal(1):
        raise ValueError("train_ratio must satisfy 0 < ratio < 1")
    pairs = tuple(
        (short_window, long_window)
        for short_window in sorted(set(short_windows))
        for long_window in sorted(set(long_windows))
        if 0 < short_window < long_window
    )
    if not pairs:
        raise ValueError("no valid MA window pairs")

    split_index = int(Decimal(len(candles)) * train_ratio)
    train_candles = list(candles[:split_index])
    validation_candles = list(candles[split_index:])
    required = max(long_window for _, long_window in pairs) + 2
    if min(len(train_candles), len(validation_candles)) < required:
        raise ValueError(
            f"each partition needs at least {required} candles for the largest window"
        )

    candidates = [
        _candidate(
            train_candles=train_candles,
            validation_candles=validation_candles,
            short_window=short_window,
            long_window=long_window,
            quantity=quantity,
            initial_cash=initial_cash,
            slippage_rate=slippage_rate,
        )
        for short_window, long_window in pairs
    ]
    train_order = sorted(candidates, key=lambda item: _rank_key(item.train))
    train_ranks = {
        (item.short_window, item.long_window): rank
        for rank, item in enumerate(train_order, start=1)
    }
    validation_order = sorted(
        candidates, key=lambda item: _rank_key(item.validation)
    )
    validation_ranks = {
        (item.short_window, item.long_window): rank
        for rank, item in enumerate(validation_order, start=1)
    }
    ranked = tuple(
        replace(
            item,
            train_rank=train_ranks[(item.short_window, item.long_window)],
            validation_rank=validation_ranks[(item.short_window, item.long_window)],
        )
        for item in train_order
    )
    selected = ranked[0]
    first = candles[0]
    return WalkForwardResult(
        symbol=first.symbol,
        interval=first.interval,
        train_candle_count=len(train_candles),
        validation_candle_count=len(validation_candles),
        train_ratio=train_ratio,
        selected_short_window=selected.short_window,
        selected_long_window=selected.long_window,
        selected_overfit_warning=selected.overfit_warning,
        candidates=ranked,
    )


def _candidate(
    *,
    train_candles: list[Candle],
    validation_candles: list[Candle],
    short_window: int,
    long_window: int,
    quantity: Decimal,
    initial_cash: Decimal,
    slippage_rate: Decimal,
) -> WalkForwardCandidate:
    common = {
        "quantity": quantity,
        "initial_cash": initial_cash,
        "short_window": short_window,
        "long_window": long_window,
        "slippage_rate": slippage_rate,
    }
    train = _metrics(run_ma_backtest(candles=train_candles, **common))
    validation = _metrics(
        run_ma_backtest(candles=validation_candles, **common)
    )
    return WalkForwardCandidate(
        short_window=short_window,
        long_window=long_window,
        train_rank=0,
        validation_rank=0,
        overfit_warning=(
            train.excess_return_rate > 0 and validation.excess_return_rate <= 0
        )
        or train.completed_trades == 0
        or validation.completed_trades == 0,
        train=train,
        validation=validation,
    )


def _metrics(result: BacktestResult) -> WalkForwardMetrics:
    return WalkForwardMetrics(
        total_return_rate=result.total_return_rate,
        buy_hold_return_rate=result.buy_hold_return_rate,
        excess_return_rate=result.excess_return_rate,
        max_drawdown_rate=result.max_drawdown_rate,
        completed_trades=result.completed_trades,
        win_rate=result.win_rate,
        total_costs=result.total_costs,
    )


def _rank_key(metrics: WalkForwardMetrics) -> tuple[Decimal, Decimal, Decimal]:
    return (
        -metrics.excess_return_rate,
        metrics.max_drawdown_rate,
        -metrics.total_return_rate,
    )
