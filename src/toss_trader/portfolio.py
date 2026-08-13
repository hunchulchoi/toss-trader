from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .paper import PaperLedgerStore
from .repository import MarketRepository


@dataclass(frozen=True, slots=True)
class DailyPortfolioPerformance:
    daily_return_rate: Decimal
    currency_returns: dict[str, Decimal]


class PortfolioPerformance:
    def __init__(
        self, *, ledger: PaperLedgerStore, market_repository: MarketRepository
    ) -> None:
        self._ledger = ledger
        self._market_repository = market_repository

    def open_position_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._ledger.position_quantities()))

    def daily(self) -> DailyPortfolioPerformance:
        positions = self._ledger.position_quantities()
        if not positions:
            return DailyPortfolioPerformance(
                daily_return_rate=Decimal(0), currency_returns={}
            )

        values: dict[str, list[Decimal]] = {}
        for symbol, quantity in positions.items():
            if quantity < 0:
                raise ValueError(f"negative paper position is not supported: {symbol}")
            candles = self._market_repository.latest_candles(symbol, "1d", limit=2)
            if len(candles) == 1:
                minute_marks = self._market_repository.latest_candles(
                    symbol, "1m", limit=1
                )
                if len(minute_marks) != 1:
                    raise ValueError(
                        f"daily baseline or latest minute mark required for {symbol}"
                    )
                current = minute_marks[0]
                if candles[0].currency.upper() != current.currency.upper():
                    raise ValueError(f"market candle currency changed for {symbol}")
                previous_value = self._ledger.position_notional(symbol)
                if previous_value <= 0:
                    raise ValueError(f"paper cost basis must be positive for {symbol}")
                currency = current.currency.upper()
                totals = values.setdefault(currency, [Decimal(0), Decimal(0)])
                totals[0] += previous_value
                totals[1] += quantity * current.close_price
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

        currency_returns = {
            currency: current / previous - Decimal(1)
            for currency, (previous, current) in values.items()
            if previous > 0
        }
        if len(currency_returns) != len(values):
            raise ValueError("previous portfolio value must be positive")
        return DailyPortfolioPerformance(
            daily_return_rate=min(currency_returns.values(), default=Decimal(0)),
            currency_returns=currency_returns,
        )
