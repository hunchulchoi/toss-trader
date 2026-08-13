from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .paper import PaperLedgerStore
from .repository import MarketRepository


@dataclass(frozen=True, slots=True)
class DailyPortfolioPerformance:
    daily_return_rate: Decimal
    currency_returns: dict[str, Decimal]
    equity: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    total_costs: Decimal = Decimal(0)


class PortfolioPerformance:
    def __init__(
        self,
        *,
        ledger: PaperLedgerStore,
        market_repository: MarketRepository,
        initial_cash: Decimal = Decimal(1000000),
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        self._ledger = ledger
        self._market_repository = market_repository
        self._initial_cash = initial_cash

    def open_position_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._ledger.position_quantities()))

    def daily(self, *, now: datetime | None = None) -> DailyPortfolioPerformance:
        captured_at = now or datetime.now(UTC)
        positions = self._ledger.position_quantities()
        values: dict[str, list[Decimal]] = {}
        market_values: dict[str, Decimal] = {}
        for symbol, quantity in positions.items():
            if quantity < 0:
                raise ValueError(f"negative paper position is not supported: {symbol}")
            candles = self._market_repository.latest_candles(symbol, "1d", limit=2)
            if len(candles) <= 1:
                minute_marks = self._market_repository.latest_candles(
                    symbol, "1m", limit=1
                )
                if len(minute_marks) != 1:
                    raise ValueError(f"latest market mark required for {symbol}")
                current = minute_marks[0]
                if (
                    candles
                    and candles[0].currency.upper() != current.currency.upper()
                ):
                    raise ValueError(f"market candle currency changed for {symbol}")
                previous_value = self._ledger.position_notional(symbol)
                if previous_value <= 0:
                    raise ValueError(f"paper cost basis must be positive for {symbol}")
                currency = current.currency.upper()
                totals = values.setdefault(currency, [Decimal(0), Decimal(0)])
                totals[0] += previous_value
                totals[1] += quantity * current.close_price
                market_values[symbol] = quantity * current.close_price
                continue
            if len(candles) != 2:
                raise ValueError(f"two daily candles required for {symbol}")
            previous, current = candles
            if previous.currency.upper() != current.currency.upper():
                raise ValueError(f"daily candle currency changed for {symbol}")
            currency = current.currency.upper()
            totals = values.setdefault(currency, [Decimal(0), Decimal(0)])
            totals[0] += quantity * previous.close_price
            totals[1] += quantity * current.close_price
            market_values[symbol] = quantity * current.close_price

        currency_returns = {
            currency: current / previous - Decimal(1)
            for currency, (previous, current) in values.items()
            if previous > 0
        }
        if len(currency_returns) != len(values):
            raise ValueError("previous portfolio value must be positive")
        accountings = self._ledger.position_accountings()
        realized_pnl = sum(
            (item.realized_pnl for item in accountings.values()), Decimal(0)
        )
        cost_basis = sum(
            (item.cost_basis for item in accountings.values()), Decimal(0)
        )
        total_costs = sum(
            (item.total_costs for item in accountings.values()), Decimal(0)
        )
        market_value = sum(market_values.values(), Decimal(0))
        cash = self._ledger.cash_balance(self._initial_cash)
        equity = cash + market_value
        unrealized_pnl = market_value - cost_basis
        baseline = self._ledger.daily_equity_baseline(captured_at)
        if baseline is None and len(values) <= 1:
            previous_market_value = sum(
                (previous for previous, _ in values.values()), Decimal(0)
            )
            baseline = cash + previous_market_value
            self._ledger.record_daily_equity_baseline(
                captured_at=captured_at, equity=baseline
            )
        if baseline is not None and baseline > 0 and len(values) <= 1:
            daily_return_rate = equity / baseline - Decimal(1)
        else:
            daily_return_rate = min(currency_returns.values(), default=Decimal(0))
        self._ledger.record_portfolio_snapshot(
            captured_at=captured_at,
            equity=equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_costs=total_costs,
        )
        return DailyPortfolioPerformance(
            daily_return_rate=daily_return_rate,
            currency_returns=currency_returns,
            equity=equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_costs=total_costs,
        )
