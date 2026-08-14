import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Candle, Side
from toss_trader.portfolio_backtest import run_ma_portfolio_backtest


def _candles(
    symbol: str,
    closes: list[int],
    *,
    opens: list[int] | None = None,
    offset: timedelta = timedelta(0),
) -> list[Candle]:
    started_at = datetime(2026, 1, 1, tzinfo=UTC) + offset
    open_prices = opens or closes
    return [
        Candle(
            symbol=symbol,
            interval="1d",
            timestamp=started_at + timedelta(days=index),
            open_price=Decimal(open_prices[index]),
            high_price=Decimal(max(open_prices[index], close)),
            low_price=Decimal(min(open_prices[index], close)),
            close_price=Decimal(close),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index, close in enumerate(closes)
    ]


class MovingAveragePortfolioBacktestTest(unittest.TestCase):
    def test_shares_cash_and_orders_simultaneous_buys_by_symbol(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles(
                    "005930",
                    [100, 100, 100, 120, 120, 120, 80, 70],
                    opens=[100, 100, 100, 100, 100, 120, 120, 80],
                ),
                "000660": _candles(
                    "000660",
                    [100, 100, 100, 120, 120, 120, 80, 70],
                    opens=[100, 100, 100, 100, 120, 120, 120, 80],
                ),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(150),
            short_window=2,
            long_window=3,
        )

        self.assertEqual(
            [trade.symbol for trade in result.trades], ["000660", "000660"]
        )
        self.assertEqual([trade.side for trade in result.trades], [Side.BUY, Side.SELL])
        self.assertEqual(result.final_cash, Decimal(110))
        self.assertEqual(result.final_equity, Decimal(110))
        self.assertEqual(result.realized_pnl, Decimal(-40))
        self.assertEqual(result.completed_trades, 1)
        self.assertEqual(result.insufficient_cash_buys, 1)
        positions = {position.symbol: position for position in result.positions}
        self.assertEqual(positions["005930"].trade_count, 0)
        self.assertEqual(positions["005930"].insufficient_cash_buys, 1)
        self.assertEqual(positions["000660"].realized_pnl, Decimal(-40))

    def test_executes_each_signal_at_that_symbols_next_open(self) -> None:
        delayed = timedelta(hours=12)
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles("005930", [100, 100, 100, 120, 130]),
                "000660": _candles("000660", [200, 200, 200, 240, 260], offset=delayed),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
        )

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[0].symbol, "005930")
        self.assertEqual(result.trades[0].executed_at.hour, 0)
        self.assertEqual(result.trades[1].symbol, "000660")
        self.assertEqual(result.trades[1].executed_at.hour, 12)
        self.assertEqual(result.position_market_value, Decimal(390))
        self.assertEqual(result.unrealized_pnl, Decimal(0))
        self.assertEqual(result.buy_hold_return_rate, Decimal("0.3"))
        positions = {position.symbol: position for position in result.positions}
        self.assertEqual(positions["005930"].average_cost, Decimal(130))
        self.assertEqual(positions["000660"].average_cost, Decimal(260))

    def test_aggregates_toss_costs_and_slippage(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles(
                    "005930",
                    [10000, 10000, 10000, 12000, 12000, 12000, 8000, 7000],
                    opens=[10000, 10000, 10000, 10000, 12100, 12000, 12000, 7900],
                ),
                "000660": _candles("000660", [10000] * 8),
            },
            quantity=Decimal(10),
            initial_cash=Decimal(1000000),
            short_window=2,
            long_window=3,
            slippage_rate=Decimal("0.01"),
        )

        self.assertEqual(result.total_costs, Decimal(185))
        self.assertEqual(result.realized_pnl, Decimal(-44185))
        self.assertEqual(result.final_equity, Decimal(955815))
        self.assertEqual(result.max_drawdown_rate, Decimal("0.044185"))

    def test_rejects_symbol_key_mismatch(self) -> None:
        candles = _candles("005930", [10, 10, 10, 12])
        with self.assertRaisesRegex(ValueError, "symbol key"):
            run_ma_portfolio_backtest(
                candles_by_symbol={"000660": candles},
                quantity=Decimal(1),
                initial_cash=Decimal(100),
                short_window=2,
                long_window=3,
            )

    def test_rejects_mixed_currency(self) -> None:
        candles = _candles("005930", [10, 10, 10, 12])
        usd = [
            Candle(
                symbol="AAPL",
                interval=item.interval,
                timestamp=item.timestamp,
                open_price=item.open_price,
                high_price=item.high_price,
                low_price=item.low_price,
                close_price=item.close_price,
                volume=item.volume,
                currency="USD",
            )
            for item in candles
        ]
        with self.assertRaisesRegex(ValueError, "currency"):
            run_ma_portfolio_backtest(
                candles_by_symbol={"005930": candles, "AAPL": usd},
                quantity=Decimal(1),
                initial_cash=Decimal(100),
                short_window=2,
                long_window=3,
            )

    def test_rejects_an_insufficient_symbol(self) -> None:
        with self.assertRaisesRegex(ValueError, "000660: need at least 4 candles"):
            run_ma_portfolio_backtest(
                candles_by_symbol={
                    "005930": _candles("005930", [10, 10, 10, 12]),
                    "000660": _candles("000660", [10, 10, 10]),
                },
                quantity=Decimal(1),
                initial_cash=Decimal(100),
                short_window=2,
                long_window=3,
            )


if __name__ == "__main__":
    unittest.main()
