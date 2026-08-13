from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from .models import Candle, Side, TradeSignal
from .paper import toss_trade_costs
from .strategy import ma_crossover_signal


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    side: Side
    executed_at: datetime
    price: Decimal
    quantity: Decimal
    commission: Decimal
    tax: Decimal
    realized_pnl: Decimal

    @property
    def total_costs(self) -> Decimal:
        return self.commission + self.tax


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    interval: str
    started_at: datetime
    finished_at: datetime
    candle_count: int
    initial_cash: Decimal
    final_equity: Decimal
    total_return_rate: Decimal
    buy_hold_return_rate: Decimal
    excess_return_rate: Decimal
    max_drawdown_rate: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_costs: Decimal
    position_quantity: Decimal
    completed_trades: int
    winning_trades: int
    win_rate: Decimal
    slippage_rate: Decimal
    trades: tuple[BacktestTrade, ...]


def run_ma_backtest(
    *,
    candles: Sequence[Candle],
    quantity: Decimal,
    initial_cash: Decimal,
    short_window: int = 20,
    long_window: int = 60,
    slippage_rate: Decimal = Decimal(0),
) -> BacktestResult:
    """Replay close signals as long-only orders at the next candle open."""
    _validate_inputs(
        candles=candles,
        quantity=quantity,
        initial_cash=initial_cash,
        short_window=short_window,
        long_window=long_window,
        slippage_rate=slippage_rate,
    )
    first = candles[0]
    cash = initial_cash
    position = Decimal(0)
    cost_basis = Decimal(0)
    realized_pnl = Decimal(0)
    total_costs = Decimal(0)
    peak_equity = initial_cash
    max_drawdown_rate = Decimal(0)
    winning_trades = 0
    completed_trades = 0
    trades: list[BacktestTrade] = []
    pending_signal: TradeSignal | None = None

    for index, candle in enumerate(candles):
        if pending_signal is not None:
            execution_price = _execution_price(
                candle.open_price, pending_signal.side, slippage_rate
            )
            execution_signal = TradeSignal(
                signal_id=pending_signal.signal_id,
                symbol=pending_signal.symbol,
                side=pending_signal.side,
                reference_price=execution_price,
                quantity=(position if pending_signal.side is Side.SELL else quantity),
                reason=pending_signal.reason,
            )
            costs = toss_trade_costs(execution_signal)
            if execution_signal.side is Side.BUY and position == 0:
                required_cash = execution_signal.notional + costs.total
                if required_cash <= cash:
                    cash -= required_cash
                    position = quantity
                    cost_basis = required_cash
                    total_costs += costs.total
                    trades.append(
                        _trade(
                            execution_signal,
                            candle.timestamp,
                            costs.commission,
                            costs.tax,
                        )
                    )
            elif execution_signal.side is Side.SELL and position > 0:
                proceeds = execution_signal.notional - costs.total
                trade_pnl = proceeds - cost_basis
                cash += proceeds
                position = Decimal(0)
                cost_basis = Decimal(0)
                realized_pnl += trade_pnl
                total_costs += costs.total
                completed_trades += 1
                if trade_pnl > 0:
                    winning_trades += 1
                trades.append(
                    _trade(
                        execution_signal,
                        candle.timestamp,
                        costs.commission,
                        costs.tax,
                        realized_pnl=trade_pnl,
                    )
                )
            pending_signal = None

        equity = cash + position * candle.close_price
        peak_equity = max(peak_equity, equity)
        drawdown_rate = (peak_equity - equity) / peak_equity
        max_drawdown_rate = max(max_drawdown_rate, drawdown_rate)

        if index >= long_window:
            window = candles[index - long_window : index + 1]
            signal = ma_crossover_signal(
                symbol=first.symbol,
                closes=[item.close_price for item in window],
                as_of=candle.timestamp,
                quantity=quantity,
                short_window=short_window,
                long_window=long_window,
            )
            if signal is not None and (
                (signal.side is Side.BUY and position == 0)
                or (signal.side is Side.SELL and position > 0)
            ):
                pending_signal = signal

    last = candles[-1]
    final_equity = cash + position * last.close_price
    total_return_rate = (final_equity - initial_cash) / initial_cash
    buy_hold_return_rate = last.close_price / first.close_price - Decimal(1)
    unrealized_pnl = (
        position * last.close_price - cost_basis if position > 0 else Decimal(0)
    )
    return BacktestResult(
        symbol=first.symbol,
        interval=first.interval,
        started_at=first.timestamp,
        finished_at=last.timestamp,
        candle_count=len(candles),
        initial_cash=initial_cash,
        final_equity=final_equity,
        total_return_rate=total_return_rate,
        buy_hold_return_rate=buy_hold_return_rate,
        excess_return_rate=total_return_rate - buy_hold_return_rate,
        max_drawdown_rate=max_drawdown_rate,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_costs=total_costs,
        position_quantity=position,
        completed_trades=completed_trades,
        winning_trades=winning_trades,
        win_rate=(
            Decimal(winning_trades) / Decimal(completed_trades)
            if completed_trades
            else Decimal(0)
        ),
        slippage_rate=slippage_rate,
        trades=tuple(trades),
    )


def _execution_price(
    open_price: Decimal, side: Side, slippage_rate: Decimal
) -> Decimal:
    direction = Decimal(1) if side is Side.BUY else Decimal(-1)
    return open_price * (Decimal(1) + direction * slippage_rate)


def _trade(
    signal: TradeSignal,
    executed_at: datetime,
    commission: Decimal,
    tax: Decimal,
    *,
    realized_pnl: Decimal = Decimal(0),
) -> BacktestTrade:
    return BacktestTrade(
        side=signal.side,
        executed_at=executed_at,
        price=signal.reference_price,
        quantity=signal.quantity,
        commission=commission,
        tax=tax,
        realized_pnl=realized_pnl,
    )


def _validate_inputs(
    *,
    candles: Sequence[Candle],
    quantity: Decimal,
    initial_cash: Decimal,
    short_window: int,
    long_window: int,
    slippage_rate: Decimal,
) -> None:
    if not 0 < short_window < long_window:
        raise ValueError("windows must satisfy 0 < short_window < long_window")
    if len(candles) < long_window + 1:
        raise ValueError(f"need at least {long_window + 1} candles")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if not Decimal(0) <= slippage_rate < Decimal(1):
        raise ValueError("slippage_rate must satisfy 0 <= rate < 1")
    first = candles[0]
    if any(
        candle.symbol != first.symbol or candle.interval != first.interval
        for candle in candles
    ):
        raise ValueError("candles must share one symbol and interval")
    if any(
        previous.timestamp >= current.timestamp
        for previous, current in pairwise(candles)
    ):
        raise ValueError("candle timestamps must be strictly increasing")
