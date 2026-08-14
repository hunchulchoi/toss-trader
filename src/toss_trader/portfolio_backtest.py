from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from .models import Candle, Side, TradeSignal
from .paper import toss_trade_costs
from .strategy import ma_crossover_signal

SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class PortfolioBacktestTrade:
    symbol: str
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
class PortfolioBacktestPosition:
    symbol: str
    candle_count: int
    quantity: Decimal
    cost_basis: Decimal
    average_cost: Decimal
    market_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_costs: Decimal
    trade_count: int
    completed_trades: int
    winning_trades: int
    insufficient_cash_buys: int
    max_open_position_rejections: int
    max_daily_buy_rejections: int
    max_position_notional_rejections: int
    max_order_notional_rejections: int


@dataclass(frozen=True, slots=True)
class PortfolioTimelinePosition:
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    market_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_costs: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioBacktestSnapshot:
    trading_date: date
    captured_at: datetime
    cash: Decimal
    position_market_value: Decimal
    equity: Decimal
    total_return_rate: Decimal
    drawdown_rate: Decimal
    max_drawdown_rate: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_costs: Decimal
    positions: tuple[PortfolioTimelinePosition, ...]


@dataclass(frozen=True, slots=True)
class PortfolioBacktestResult:
    symbols: tuple[str, ...]
    interval: str
    currency: str
    started_at: datetime
    finished_at: datetime
    candle_count: int
    initial_cash: Decimal
    final_cash: Decimal
    position_market_value: Decimal
    final_equity: Decimal
    total_return_rate: Decimal
    buy_hold_return_rate: Decimal
    excess_return_rate: Decimal
    max_drawdown_rate: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_costs: Decimal
    completed_trades: int
    winning_trades: int
    win_rate: Decimal
    insufficient_cash_buys: int
    max_open_position_rejections: int
    max_daily_buy_rejections: int
    max_position_notional_rejections: int
    max_order_notional_rejections: int
    slippage_rate: Decimal
    positions: tuple[PortfolioBacktestPosition, ...]
    trades: tuple[PortfolioBacktestTrade, ...]
    timeline: tuple[PortfolioBacktestSnapshot, ...]


@dataclass(slots=True)
class _PositionState:
    quantity: Decimal = Decimal(0)
    cost_basis: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    total_costs: Decimal = Decimal(0)
    trade_count: int = 0
    completed_trades: int = 0
    winning_trades: int = 0
    insufficient_cash_buys: int = 0
    max_open_position_rejections: int = 0
    max_daily_buy_rejections: int = 0
    max_position_notional_rejections: int = 0
    max_order_notional_rejections: int = 0


def run_ma_portfolio_backtest(
    *,
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    quantity: Decimal,
    initial_cash: Decimal,
    short_window: int = 20,
    long_window: int = 60,
    slippage_rate: Decimal = Decimal(0),
    max_open_positions: int | None = None,
    max_daily_buys: int | None = None,
    max_position_notional: Decimal | None = None,
    max_order_notional: Decimal | None = None,
) -> PortfolioBacktestResult:
    """Replay per-symbol MA signals against one shared cash balance."""
    symbols, interval, currency = _validate_inputs(
        candles_by_symbol=candles_by_symbol,
        quantity=quantity,
        initial_cash=initial_cash,
        short_window=short_window,
        long_window=long_window,
        slippage_rate=slippage_rate,
        max_open_positions=max_open_positions,
        max_daily_buys=max_daily_buys,
        max_position_notional=max_position_notional,
        max_order_notional=max_order_notional,
    )
    states = {symbol: _PositionState() for symbol in symbols}
    pending: dict[str, TradeSignal] = {}
    last_prices: dict[str, Decimal] = {}
    cash = initial_cash
    peak_equity = initial_cash
    max_drawdown_rate = Decimal(0)
    trades: list[PortfolioBacktestTrade] = []
    daily_snapshots: dict[date, PortfolioBacktestSnapshot] = {}
    daily_buy_counts: dict[date, int] = {}

    events: dict[datetime, list[tuple[str, int, Candle]]] = {}
    for symbol in symbols:
        for index, candle in enumerate(candles_by_symbol[symbol]):
            events.setdefault(candle.timestamp, []).append((symbol, index, candle))

    for timestamp in sorted(events):
        timestamp_events = sorted(events[timestamp], key=lambda item: item[0])
        for symbol, _, candle in timestamp_events:
            signal = pending.pop(symbol, None)
            if signal is not None:
                trading_date = candle.timestamp.date()
                rejections = _risk_rejections(
                    signal=signal,
                    quantity=quantity,
                    state=states[symbol],
                    open_position_count=sum(
                        state.quantity > 0 for state in states.values()
                    ),
                    daily_buy_count=daily_buy_counts.get(trading_date, 0),
                    max_open_positions=max_open_positions,
                    max_daily_buys=max_daily_buys,
                    max_position_notional=max_position_notional,
                    max_order_notional=max_order_notional,
                )
                if rejections:
                    for rejection in rejections:
                        setattr(
                            states[symbol],
                            rejection,
                            getattr(states[symbol], rejection) + 1,
                        )
                    continue
                cash, executed = _execute_pending(
                    signal=signal,
                    candle=candle,
                    quantity=quantity,
                    slippage_rate=slippage_rate,
                    cash=cash,
                    state=states[symbol],
                    trades=trades,
                )
                if executed and signal.side is Side.BUY:
                    daily_buy_counts[trading_date] = (
                        daily_buy_counts.get(trading_date, 0) + 1
                    )

        for symbol, _, candle in timestamp_events:
            last_prices[symbol] = candle.close_price

        for symbol, index, candle in timestamp_events:
            state = states[symbol]
            if index < long_window:
                continue
            symbol_candles = candles_by_symbol[symbol]
            window = symbol_candles[index - long_window : index + 1]
            signal = ma_crossover_signal(
                symbol=symbol,
                closes=[item.close_price for item in window],
                as_of=candle.timestamp,
                quantity=quantity,
                short_window=short_window,
                long_window=long_window,
            )
            if signal is not None and (
                (signal.side is Side.BUY and state.quantity == 0)
                or (signal.side is Side.SELL and state.quantity > 0)
            ):
                pending[symbol] = signal

        equity = cash + sum(
            (states[symbol].quantity * price for symbol, price in last_prices.items()),
            start=Decimal(0),
        )
        peak_equity = max(peak_equity, equity)
        drawdown_rate = (peak_equity - equity) / peak_equity
        max_drawdown_rate = max(max_drawdown_rate, drawdown_rate)
        snapshot_positions = _timeline_positions(
            symbols=symbols,
            states=states,
            last_prices=last_prices,
        )
        daily_snapshots[timestamp.astimezone(SEOUL).date()] = PortfolioBacktestSnapshot(
            trading_date=timestamp.astimezone(SEOUL).date(),
            captured_at=timestamp,
            cash=cash,
            position_market_value=sum(
                (position.market_value for position in snapshot_positions),
                start=Decimal(0),
            ),
            equity=equity,
            total_return_rate=(equity - initial_cash) / initial_cash,
            drawdown_rate=drawdown_rate,
            max_drawdown_rate=max_drawdown_rate,
            realized_pnl=sum(
                (state.realized_pnl for state in states.values()),
                start=Decimal(0),
            ),
            unrealized_pnl=sum(
                (position.unrealized_pnl for position in snapshot_positions),
                start=Decimal(0),
            ),
            total_costs=sum(
                (state.total_costs for state in states.values()),
                start=Decimal(0),
            ),
            positions=snapshot_positions,
        )

    positions = tuple(
        _position_result(
            symbol=symbol,
            candles=candles_by_symbol[symbol],
            state=states[symbol],
        )
        for symbol in symbols
    )
    position_market_value = sum(
        (position.market_value for position in positions), start=Decimal(0)
    )
    final_equity = cash + position_market_value
    realized_pnl = sum(
        (position.realized_pnl for position in positions), start=Decimal(0)
    )
    unrealized_pnl = sum(
        (position.unrealized_pnl for position in positions), start=Decimal(0)
    )
    total_costs = sum(
        (position.total_costs for position in positions), start=Decimal(0)
    )
    completed_trades = sum(position.completed_trades for position in positions)
    winning_trades = sum(position.winning_trades for position in positions)
    insufficient_cash_buys = sum(
        position.insufficient_cash_buys for position in positions
    )
    max_open_position_rejections = sum(
        position.max_open_position_rejections for position in positions
    )
    max_daily_buy_rejections = sum(
        position.max_daily_buy_rejections for position in positions
    )
    max_position_notional_rejections = sum(
        position.max_position_notional_rejections for position in positions
    )
    max_order_notional_rejections = sum(
        position.max_order_notional_rejections for position in positions
    )
    total_return_rate = (final_equity - initial_cash) / initial_cash
    buy_hold_return_rate = sum(
        (
            candles_by_symbol[symbol][-1].close_price
            / candles_by_symbol[symbol][0].close_price
            - Decimal(1)
            for symbol in symbols
        ),
        start=Decimal(0),
    ) / Decimal(len(symbols))
    return PortfolioBacktestResult(
        symbols=symbols,
        interval=interval,
        currency=currency,
        started_at=min(candles_by_symbol[symbol][0].timestamp for symbol in symbols),
        finished_at=max(candles_by_symbol[symbol][-1].timestamp for symbol in symbols),
        candle_count=sum(len(candles_by_symbol[symbol]) for symbol in symbols),
        initial_cash=initial_cash,
        final_cash=cash,
        position_market_value=position_market_value,
        final_equity=final_equity,
        total_return_rate=total_return_rate,
        buy_hold_return_rate=buy_hold_return_rate,
        excess_return_rate=total_return_rate - buy_hold_return_rate,
        max_drawdown_rate=max_drawdown_rate,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_costs=total_costs,
        completed_trades=completed_trades,
        winning_trades=winning_trades,
        win_rate=(
            Decimal(winning_trades) / Decimal(completed_trades)
            if completed_trades
            else Decimal(0)
        ),
        insufficient_cash_buys=insufficient_cash_buys,
        max_open_position_rejections=max_open_position_rejections,
        max_daily_buy_rejections=max_daily_buy_rejections,
        max_position_notional_rejections=max_position_notional_rejections,
        max_order_notional_rejections=max_order_notional_rejections,
        slippage_rate=slippage_rate,
        positions=positions,
        trades=tuple(trades),
        timeline=tuple(daily_snapshots.values()),
    )


def _execute_pending(
    *,
    signal: TradeSignal,
    candle: Candle,
    quantity: Decimal,
    slippage_rate: Decimal,
    cash: Decimal,
    state: _PositionState,
    trades: list[PortfolioBacktestTrade],
) -> tuple[Decimal, bool]:
    direction = Decimal(1) if signal.side is Side.BUY else Decimal(-1)
    price = candle.open_price * (Decimal(1) + direction * slippage_rate)
    execution_quantity = state.quantity if signal.side is Side.SELL else quantity
    execution_signal = TradeSignal(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        side=signal.side,
        reference_price=price,
        quantity=execution_quantity,
        reason=signal.reason,
    )
    costs = toss_trade_costs(execution_signal)
    if signal.side is Side.BUY and state.quantity == 0:
        required_cash = execution_signal.notional + costs.total
        if required_cash > cash:
            state.insufficient_cash_buys += 1
            return cash, False
        cash -= required_cash
        state.quantity = quantity
        state.cost_basis = required_cash
        realized_pnl = Decimal(0)
    elif signal.side is Side.SELL and state.quantity > 0:
        proceeds = execution_signal.notional - costs.total
        realized_pnl = proceeds - state.cost_basis
        cash += proceeds
        state.quantity = Decimal(0)
        state.cost_basis = Decimal(0)
        state.realized_pnl += realized_pnl
        state.completed_trades += 1
        if realized_pnl > 0:
            state.winning_trades += 1
    else:
        return cash, False
    state.total_costs += costs.total
    state.trade_count += 1
    trades.append(
        PortfolioBacktestTrade(
            symbol=signal.symbol,
            side=signal.side,
            executed_at=candle.timestamp,
            price=price,
            quantity=execution_quantity,
            commission=costs.commission,
            tax=costs.tax,
            realized_pnl=realized_pnl,
        )
    )
    return cash, True


def _risk_rejections(
    *,
    signal: TradeSignal,
    quantity: Decimal,
    state: _PositionState,
    open_position_count: int,
    daily_buy_count: int,
    max_open_positions: int | None,
    max_daily_buys: int | None,
    max_position_notional: Decimal | None,
    max_order_notional: Decimal | None,
) -> tuple[str, ...]:
    rejections: list[str] = []
    signal_notional = quantity * signal.reference_price
    if max_order_notional is not None and signal_notional > max_order_notional:
        rejections.append("max_order_notional_rejections")
    if signal.side is not Side.BUY:
        return tuple(rejections)
    if (
        max_position_notional is not None
        and state.quantity * signal.reference_price + signal_notional
        > max_position_notional
    ):
        rejections.append("max_position_notional_rejections")
    if max_daily_buys is not None and daily_buy_count >= max_daily_buys:
        rejections.append("max_daily_buy_rejections")
    if (
        max_open_positions is not None
        and state.quantity <= 0
        and open_position_count >= max_open_positions
    ):
        rejections.append("max_open_position_rejections")
    return tuple(rejections)


def _position_result(
    *, symbol: str, candles: Sequence[Candle], state: _PositionState
) -> PortfolioBacktestPosition:
    market_price = candles[-1].close_price
    market_value = state.quantity * market_price
    return PortfolioBacktestPosition(
        symbol=symbol,
        candle_count=len(candles),
        quantity=state.quantity,
        cost_basis=state.cost_basis,
        average_cost=(
            state.cost_basis / state.quantity if state.quantity > 0 else Decimal(0)
        ),
        market_price=market_price,
        market_value=market_value,
        realized_pnl=state.realized_pnl,
        unrealized_pnl=(
            market_value - state.cost_basis if state.quantity > 0 else Decimal(0)
        ),
        total_costs=state.total_costs,
        trade_count=state.trade_count,
        completed_trades=state.completed_trades,
        winning_trades=state.winning_trades,
        insufficient_cash_buys=state.insufficient_cash_buys,
        max_open_position_rejections=state.max_open_position_rejections,
        max_daily_buy_rejections=state.max_daily_buy_rejections,
        max_position_notional_rejections=state.max_position_notional_rejections,
        max_order_notional_rejections=state.max_order_notional_rejections,
    )


def _timeline_positions(
    *,
    symbols: Sequence[str],
    states: Mapping[str, _PositionState],
    last_prices: Mapping[str, Decimal],
) -> tuple[PortfolioTimelinePosition, ...]:
    positions: list[PortfolioTimelinePosition] = []
    for symbol in symbols:
        market_price = last_prices.get(symbol)
        if market_price is None:
            continue
        state = states[symbol]
        market_value = state.quantity * market_price
        positions.append(
            PortfolioTimelinePosition(
                symbol=symbol,
                quantity=state.quantity,
                average_cost=(
                    state.cost_basis / state.quantity
                    if state.quantity > 0
                    else Decimal(0)
                ),
                market_price=market_price,
                market_value=market_value,
                realized_pnl=state.realized_pnl,
                unrealized_pnl=(
                    market_value - state.cost_basis
                    if state.quantity > 0
                    else Decimal(0)
                ),
                total_costs=state.total_costs,
            )
        )
    return tuple(positions)


def _validate_inputs(
    *,
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    quantity: Decimal,
    initial_cash: Decimal,
    short_window: int,
    long_window: int,
    slippage_rate: Decimal,
    max_open_positions: int | None,
    max_daily_buys: int | None,
    max_position_notional: Decimal | None,
    max_order_notional: Decimal | None,
) -> tuple[tuple[str, ...], str, str]:
    if not candles_by_symbol:
        raise ValueError("candles_by_symbol must not be empty")
    if not 0 < short_window < long_window:
        raise ValueError("windows must satisfy 0 < short_window < long_window")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if not Decimal(0) <= slippage_rate < Decimal(1):
        raise ValueError("slippage_rate must satisfy 0 <= rate < 1")
    if max_open_positions is not None and max_open_positions <= 0:
        raise ValueError("max_open_positions must be positive")
    if max_daily_buys is not None and max_daily_buys <= 0:
        raise ValueError("max_daily_buys must be positive")
    if max_position_notional is not None and max_position_notional <= 0:
        raise ValueError("max_position_notional must be positive")
    if max_order_notional is not None and max_order_notional <= 0:
        raise ValueError("max_order_notional must be positive")
    symbols = tuple(sorted(candles_by_symbol))
    intervals: set[str] = set()
    currencies: set[str] = set()
    for symbol in symbols:
        candles = candles_by_symbol[symbol]
        if len(candles) < long_window + 1:
            raise ValueError(f"{symbol}: need at least {long_window + 1} candles")
        if any(candle.symbol != symbol for candle in candles):
            raise ValueError(f"{symbol}: candles must match symbol key")
        intervals.update(candle.interval for candle in candles)
        currencies.update(candle.currency.upper() for candle in candles)
        if any(
            previous.timestamp >= current.timestamp
            for previous, current in pairwise(candles)
        ):
            raise ValueError(f"{symbol}: candle timestamps must be strictly increasing")
    if len(intervals) != 1:
        raise ValueError("all candles must share one interval")
    if len(currencies) != 1:
        raise ValueError("all candles must share one currency")
    return symbols, intervals.pop(), currencies.pop()
