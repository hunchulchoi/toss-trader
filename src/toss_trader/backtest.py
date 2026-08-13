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
    max_drawdown_rate: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_costs: Decimal
    position_quantity: Decimal
    completed_trades: int
    winning_trades: int
    win_rate: Decimal
    trades: tuple[BacktestTrade, ...]


def run_ma_backtest(
    *,
    candles: Sequence[Candle],
    quantity: Decimal,
    initial_cash: Decimal,
    short_window: int = 20,
    long_window: int = 60,
) -> BacktestResult:
    """Replay a long-only MA crossover strategy at candle close prices."""
    _validate_inputs(
        candles=candles,
        quantity=quantity,
        initial_cash=initial_cash,
        short_window=short_window,
        long_window=long_window,
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

    for index, candle in enumerate(candles):
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
            if signal is not None and signal.side is Side.BUY and position == 0:
                costs = toss_trade_costs(signal)
                required_cash = signal.notional + costs.total
                if required_cash <= cash:
                    cash -= required_cash
                    position = quantity
                    cost_basis = required_cash
                    total_costs += costs.total
                    trades.append(
                        _trade(signal, candle.timestamp, costs.commission, costs.tax)
                    )
            elif signal is not None and signal.side is Side.SELL and position > 0:
                sell_signal = TradeSignal(
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    side=signal.side,
                    reference_price=signal.reference_price,
                    quantity=position,
                    reason=signal.reason,
                )
                costs = toss_trade_costs(sell_signal)
                proceeds = sell_signal.notional - costs.total
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
                        sell_signal,
                        candle.timestamp,
                        costs.commission,
                        costs.tax,
                        realized_pnl=trade_pnl,
                    )
                )

        equity = cash + position * candle.close_price
        peak_equity = max(peak_equity, equity)
        drawdown_rate = (peak_equity - equity) / peak_equity
        max_drawdown_rate = max(max_drawdown_rate, drawdown_rate)

    last = candles[-1]
    final_equity = cash + position * last.close_price
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
        total_return_rate=(final_equity - initial_cash) / initial_cash,
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
        trades=tuple(trades),
    )


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
) -> None:
    if not 0 < short_window < long_window:
        raise ValueError("windows must satisfy 0 < short_window < long_window")
    if len(candles) < long_window + 1:
        raise ValueError(f"need at least {long_window + 1} candles")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
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
