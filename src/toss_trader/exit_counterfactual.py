from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from .models import Candle, Side, TradeSignal
from .paper import toss_trade_costs


class ExitPolicy(StrEnum):
    DEAD_CROSS = "dead-cross"
    MIN_HOLD = "min-hold"
    ATR_TRAILING = "atr-trailing"


@dataclass(frozen=True, slots=True)
class ExitVariant:
    name: str
    policy: ExitPolicy
    min_hold_bars: int = 0
    atr_period: int = 14
    atr_multiple: Decimal = Decimal("2.0")


@dataclass(frozen=True, slots=True)
class ExitCounterfactualTrade:
    symbol: str
    variant: str
    entered_at: datetime
    exited_at: datetime | None
    entry_price: Decimal
    exit_price: Decimal | None
    held_bars: int
    exit_reason: str | None
    realized_pnl: Decimal
    marked_pnl: Decimal
    actual_costs: Decimal
    maximum_favorable_excursion_rate: Decimal
    maximum_adverse_excursion_rate: Decimal


@dataclass(frozen=True, slots=True)
class ExitCounterfactualResult:
    variant: str
    policy: ExitPolicy
    symbol_count: int
    entry_count: int
    completed_trades: int
    open_trades: int
    winning_trades: int
    win_rate: Decimal
    realized_pnl: Decimal
    marked_pnl: Decimal
    actual_costs: Decimal
    profit_factor: Decimal | None
    average_holding_bars: Decimal
    loss_within_10_bars_rate: Decimal
    exit_reason_counts: tuple[tuple[str, int], ...]
    trades: tuple[ExitCounterfactualTrade, ...]


@dataclass(slots=True)
class _OpenTrade:
    entered_at: datetime
    entry_index: int
    entry_price: Decimal
    cost_basis: Decimal
    buy_costs: Decimal
    high_water: Decimal
    low_water: Decimal
    trailing_stop: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _Indicators:
    relations: tuple[int | None, ...]
    atr: tuple[Decimal | None, ...]


DEFAULT_EXIT_VARIANTS = (
    ExitVariant(name="dead-cross", policy=ExitPolicy.DEAD_CROSS),
    ExitVariant(name="min-hold-5", policy=ExitPolicy.MIN_HOLD, min_hold_bars=5),
    ExitVariant(name="min-hold-10", policy=ExitPolicy.MIN_HOLD, min_hold_bars=10),
    ExitVariant(name="min-hold-15", policy=ExitPolicy.MIN_HOLD, min_hold_bars=15),
    ExitVariant(name="atr-trailing-2", policy=ExitPolicy.ATR_TRAILING),
)


def run_exit_counterfactual_matrix(
    *,
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    variants: Sequence[ExitVariant] = DEFAULT_EXIT_VARIANTS,
    quantity: Decimal = Decimal(1),
    short_window: int = 20,
    long_window: int = 60,
    slippage_rate: Decimal = Decimal("0.0005"),
) -> tuple[ExitCounterfactualResult, ...]:
    """Compare exit policies on identical MA crossover entry rules."""
    _validate_matrix_inputs(
        candles_by_symbol=candles_by_symbol,
        variants=variants,
        quantity=quantity,
        short_window=short_window,
        long_window=long_window,
        slippage_rate=slippage_rate,
    )
    collected: dict[str, list[ExitCounterfactualTrade]] = {
        variant.name: [] for variant in variants
    }
    atr_period = next(
        (
            variant.atr_period
            for variant in variants
            if variant.policy is ExitPolicy.ATR_TRAILING
        ),
        14,
    )
    for symbol in sorted(candles_by_symbol):
        candles = tuple(candles_by_symbol[symbol])
        indicators = _indicators(
            candles,
            short_window=short_window,
            long_window=long_window,
            atr_period=atr_period,
        )
        for variant in variants:
            collected[variant.name].extend(
                _simulate_symbol(
                    candles=candles,
                    indicators=indicators,
                    variant=variant,
                    quantity=quantity,
                    slippage_rate=slippage_rate,
                )
            )
    return tuple(
        _summarize(
            variant=variant,
            symbol_count=len(candles_by_symbol),
            trades=tuple(collected[variant.name]),
        )
        for variant in variants
    )


def _simulate_symbol(
    *,
    candles: tuple[Candle, ...],
    indicators: _Indicators,
    variant: ExitVariant,
    quantity: Decimal,
    slippage_rate: Decimal,
) -> list[ExitCounterfactualTrade]:
    trades: list[ExitCounterfactualTrade] = []
    open_trade: _OpenTrade | None = None
    pending: str | None = None
    for index, candle in enumerate(candles):
        if pending == "buy" and open_trade is None:
            entry_price = candle.open_price * (Decimal(1) + slippage_rate)
            buy_costs = _trade_costs(
                symbol=candle.symbol,
                side=Side.BUY,
                quantity=quantity,
                price=entry_price,
            )
            open_trade = _OpenTrade(
                entered_at=candle.timestamp,
                entry_index=index,
                entry_price=entry_price,
                cost_basis=entry_price * quantity + buy_costs,
                buy_costs=buy_costs,
                high_water=entry_price,
                low_water=entry_price,
            )
        elif pending is not None and open_trade is not None:
            trades.append(
                _close_trade(
                    symbol=candle.symbol,
                    variant=variant,
                    open_trade=open_trade,
                    exited_at=candle.timestamp,
                    raw_exit_price=candle.open_price,
                    exit_reason=pending,
                    exit_index=index,
                    quantity=quantity,
                    slippage_rate=slippage_rate,
                )
            )
            open_trade = None
        pending = None

        if (
            open_trade is not None
            and variant.policy is ExitPolicy.ATR_TRAILING
            and open_trade.trailing_stop is not None
        ):
            raw_stop_exit: Decimal | None = None
            if candle.open_price < open_trade.trailing_stop:
                raw_stop_exit = candle.open_price
            elif candle.low_price <= open_trade.trailing_stop <= candle.open_price:
                raw_stop_exit = open_trade.trailing_stop
            if raw_stop_exit is not None:
                trades.append(
                    _close_trade(
                        symbol=candle.symbol,
                        variant=variant,
                        open_trade=open_trade,
                        exited_at=candle.timestamp,
                        raw_exit_price=raw_stop_exit,
                        exit_reason="atr-stop",
                        exit_index=index,
                        quantity=quantity,
                        slippage_rate=slippage_rate,
                    )
                )
                open_trade = None

        relation = indicators.relations[index]
        previous_relation = (
            indicators.relations[index - 1] if index > 0 else None
        )
        crossed_above = (
            relation == 1
            and previous_relation is not None
            and previous_relation <= 0
        )
        crossed_below = (
            relation == -1
            and previous_relation is not None
            and previous_relation >= 0
        )
        if open_trade is None:
            if crossed_above and index + 1 < len(candles):
                pending = "buy"
            continue

        open_trade.high_water = max(open_trade.high_water, candle.high_price)
        open_trade.low_water = min(open_trade.low_water, candle.low_price)
        held_bars = index - open_trade.entry_index + 1
        if variant.policy is ExitPolicy.DEAD_CROSS and crossed_below:
            pending = "dead-cross"
        elif (
            variant.policy is ExitPolicy.MIN_HOLD
            and held_bars >= variant.min_hold_bars
            and relation == -1
        ):
            pending = "min-hold-dead-cross"
        elif variant.policy is ExitPolicy.ATR_TRAILING:
            atr = indicators.atr[index]
            if atr is not None:
                candidate = open_trade.high_water - variant.atr_multiple * atr
                open_trade.trailing_stop = (
                    candidate
                    if open_trade.trailing_stop is None
                    else max(open_trade.trailing_stop, candidate)
                )

    if open_trade is not None:
        trades.append(
            _mark_open_trade(
                variant=variant,
                open_trade=open_trade,
                last_candle=candles[-1],
                last_index=len(candles) - 1,
                quantity=quantity,
            )
        )
    return trades


def _close_trade(
    *,
    symbol: str,
    variant: ExitVariant,
    open_trade: _OpenTrade,
    exited_at: datetime,
    raw_exit_price: Decimal,
    exit_reason: str,
    exit_index: int,
    quantity: Decimal,
    slippage_rate: Decimal,
) -> ExitCounterfactualTrade:
    exit_price = raw_exit_price * (Decimal(1) - slippage_rate)
    sell_costs = _trade_costs(
        symbol=symbol,
        side=Side.SELL,
        quantity=quantity,
        price=exit_price,
    )
    realized = exit_price * quantity - sell_costs - open_trade.cost_basis
    exit_high_water = max(open_trade.high_water, raw_exit_price)
    exit_low_water = min(open_trade.low_water, raw_exit_price)
    return ExitCounterfactualTrade(
        symbol=symbol,
        variant=variant.name,
        entered_at=open_trade.entered_at,
        exited_at=exited_at,
        entry_price=open_trade.entry_price,
        exit_price=exit_price,
        held_bars=exit_index - open_trade.entry_index,
        exit_reason=exit_reason,
        realized_pnl=realized,
        marked_pnl=realized,
        actual_costs=open_trade.buy_costs + sell_costs,
        maximum_favorable_excursion_rate=(
            exit_high_water / open_trade.entry_price - Decimal(1)
        ),
        maximum_adverse_excursion_rate=(
            open_trade.entry_price - exit_low_water
        )
        / open_trade.entry_price,
    )


def _mark_open_trade(
    *,
    variant: ExitVariant,
    open_trade: _OpenTrade,
    last_candle: Candle,
    last_index: int,
    quantity: Decimal,
) -> ExitCounterfactualTrade:
    marked = (
        last_candle.close_price * quantity
        - open_trade.cost_basis
    )
    return ExitCounterfactualTrade(
        symbol=last_candle.symbol,
        variant=variant.name,
        entered_at=open_trade.entered_at,
        exited_at=None,
        entry_price=open_trade.entry_price,
        exit_price=None,
        held_bars=last_index - open_trade.entry_index + 1,
        exit_reason=None,
        realized_pnl=Decimal(0),
        marked_pnl=marked,
        actual_costs=open_trade.buy_costs,
        maximum_favorable_excursion_rate=(
            open_trade.high_water / open_trade.entry_price - Decimal(1)
        ),
        maximum_adverse_excursion_rate=(
            open_trade.entry_price - open_trade.low_water
        )
        / open_trade.entry_price,
    )


def _summarize(
    *,
    variant: ExitVariant,
    symbol_count: int,
    trades: tuple[ExitCounterfactualTrade, ...],
) -> ExitCounterfactualResult:
    completed = tuple(trade for trade in trades if trade.exited_at is not None)
    winners = tuple(trade for trade in completed if trade.realized_pnl > 0)
    losses = tuple(trade for trade in completed if trade.realized_pnl < 0)
    gross_profit = sum(
        (trade.realized_pnl for trade in winners), start=Decimal(0)
    )
    gross_loss = -sum(
        (trade.realized_pnl for trade in losses), start=Decimal(0)
    )
    early_losses = sum(
        trade.realized_pnl < 0 and trade.held_bars <= 10 for trade in completed
    )
    reasons = Counter(
        trade.exit_reason for trade in completed if trade.exit_reason is not None
    )
    return ExitCounterfactualResult(
        variant=variant.name,
        policy=variant.policy,
        symbol_count=symbol_count,
        entry_count=len(trades),
        completed_trades=len(completed),
        open_trades=len(trades) - len(completed),
        winning_trades=len(winners),
        win_rate=(
            Decimal(len(winners)) / Decimal(len(completed))
            if completed
            else Decimal(0)
        ),
        realized_pnl=sum(
            (trade.realized_pnl for trade in completed), start=Decimal(0)
        ),
        marked_pnl=sum((trade.marked_pnl for trade in trades), start=Decimal(0)),
        actual_costs=sum((trade.actual_costs for trade in trades), start=Decimal(0)),
        profit_factor=gross_profit / gross_loss if gross_loss else None,
        average_holding_bars=(
            Decimal(sum(trade.held_bars for trade in completed))
            / Decimal(len(completed))
            if completed
            else Decimal(0)
        ),
        loss_within_10_bars_rate=(
            Decimal(early_losses) / Decimal(len(completed))
            if completed
            else Decimal(0)
        ),
        exit_reason_counts=tuple(sorted(reasons.items())),
        trades=trades,
    )


def _indicators(
    candles: tuple[Candle, ...],
    *,
    short_window: int,
    long_window: int,
    atr_period: int,
) -> _Indicators:
    closes = [candle.close_price for candle in candles]
    relations: list[int | None] = [None] * len(candles)
    short_sum = sum(closes[:short_window], start=Decimal(0))
    long_sum = sum(closes[:long_window], start=Decimal(0))
    for index in range(long_window - 1, len(candles)):
        if index > long_window - 1:
            short_sum += closes[index] - closes[index - short_window]
            long_sum += closes[index] - closes[index - long_window]
        else:
            short_sum = sum(
                closes[index - short_window + 1 : index + 1], start=Decimal(0)
            )
        short_ma = short_sum / Decimal(short_window)
        long_ma = long_sum / Decimal(long_window)
        relations[index] = 1 if short_ma > long_ma else -1 if short_ma < long_ma else 0

    true_ranges: list[Decimal] = []
    for index, candle in enumerate(candles):
        if index == 0:
            true_ranges.append(candle.high_price - candle.low_price)
        else:
            previous_close = candles[index - 1].close_price
            true_ranges.append(
                max(
                    candle.high_price - candle.low_price,
                    abs(candle.high_price - previous_close),
                    abs(candle.low_price - previous_close),
                )
            )
    atr: list[Decimal | None] = [None] * len(candles)
    if len(candles) >= atr_period:
        current = sum(true_ranges[:atr_period], start=Decimal(0)) / Decimal(
            atr_period
        )
        atr[atr_period - 1] = current
        for index in range(atr_period, len(candles)):
            current = (
                current * Decimal(atr_period - 1) + true_ranges[index]
            ) / Decimal(atr_period)
            atr[index] = current
    return _Indicators(relations=tuple(relations), atr=tuple(atr))


def _trade_costs(
    *, symbol: str, side: Side, quantity: Decimal, price: Decimal
) -> Decimal:
    signal = TradeSignal(
        signal_id=f"exit-counterfactual-{side.lower()}-{symbol}",
        symbol=symbol,
        side=side,
        reference_price=price,
        quantity=quantity,
        reason="exit counterfactual",
    )
    return toss_trade_costs(signal).total


def _validate_matrix_inputs(
    *,
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    variants: Sequence[ExitVariant],
    quantity: Decimal,
    short_window: int,
    long_window: int,
    slippage_rate: Decimal,
) -> None:
    if not candles_by_symbol:
        raise ValueError("candles_by_symbol must not be empty")
    if not variants or len({variant.name for variant in variants}) != len(variants):
        raise ValueError("variants must be non-empty with unique names")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not 0 < short_window < long_window:
        raise ValueError("windows must satisfy 0 < short_window < long_window")
    if not Decimal(0) <= slippage_rate < 1:
        raise ValueError("slippage_rate must satisfy 0 <= rate < 1")
    for variant in variants:
        if not variant.name.strip():
            raise ValueError("variant name must not be empty")
        if variant.policy is ExitPolicy.MIN_HOLD and variant.min_hold_bars <= 0:
            raise ValueError("min-hold variants need positive min_hold_bars")
        if variant.atr_period <= 0 or variant.atr_multiple <= 0:
            raise ValueError("ATR settings must be positive")
    atr_periods = {
        variant.atr_period
        for variant in variants
        if variant.policy is ExitPolicy.ATR_TRAILING
    }
    if len(atr_periods) > 1:
        raise ValueError("ATR variants must use one shared atr_period")
    for symbol, source in candles_by_symbol.items():
        candles = tuple(source)
        if len(candles) < long_window + 1:
            raise ValueError(f"{symbol} needs at least {long_window + 1} candles")
        if any(candle.symbol != symbol or candle.interval != "1m" for candle in candles):
            raise ValueError("counterfactual candles must match symbol and use 1m")
        if any(
            previous.timestamp >= current.timestamp
            for previous, current in pairwise(candles)
        ):
            raise ValueError("candle timestamps must be strictly increasing")
