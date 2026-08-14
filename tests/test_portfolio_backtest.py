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
        self.assertEqual(len(result.timeline), 5)
        final_day = result.timeline[-1]
        self.assertEqual(final_day.trading_date.isoformat(), "2026-01-05")
        self.assertEqual(final_day.equity, result.final_equity)
        self.assertEqual(final_day.cash, result.final_cash)
        self.assertEqual(final_day.position_market_value, Decimal(390))
        self.assertEqual(len(final_day.positions), 2)

    def test_keeps_only_the_last_snapshot_for_each_kst_date(self) -> None:
        candles = _candles("005930", [100, 100, 100, 120, 130])
        same_kst_day = [
            Candle(
                symbol=item.symbol,
                interval="1m",
                timestamp=datetime(2026, 1, 2, index, tzinfo=UTC),
                open_price=item.open_price,
                high_price=item.high_price,
                low_price=item.low_price,
                close_price=item.close_price,
                volume=item.volume,
                currency=item.currency,
            )
            for index, item in enumerate(candles)
        ]

        result = run_ma_portfolio_backtest(
            candles_by_symbol={"005930": same_kst_day},
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
        )

        self.assertEqual(len(result.timeline), 1)
        self.assertEqual(result.timeline[0].captured_at.hour, 4)

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

    def test_applies_open_position_limit_in_execution_order(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles("005930", [100, 100, 100, 120, 130]),
                "000660": _candles("000660", [100, 100, 100, 120, 130]),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
            max_open_positions=1,
        )

        self.assertEqual([trade.symbol for trade in result.trades], ["000660"])
        self.assertEqual(result.max_open_position_rejections, 1)
        positions = {position.symbol: position for position in result.positions}
        self.assertEqual(positions["005930"].max_open_position_rejections, 1)

    def test_applies_daily_buy_limit_by_utc_execution_date(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles("005930", [100, 100, 100, 120, 130]),
                "000660": _candles("000660", [100, 100, 100, 120, 130]),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
            max_daily_buys=1,
        )

        self.assertEqual([trade.symbol for trade in result.trades], ["000660"])
        self.assertEqual(result.max_daily_buy_rejections, 1)

    def test_resets_daily_buy_limit_on_next_utc_date(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles("005930", [100, 100, 100, 120, 130]),
                "000660": _candles(
                    "000660",
                    [100, 100, 100, 120, 130],
                    offset=timedelta(hours=25),
                ),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
            max_daily_buys=1,
        )

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.max_daily_buy_rejections, 0)

    def test_applies_position_notional_limit_to_execution_price(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles("005930", [100, 100, 100, 120, 130]),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
            max_position_notional=Decimal(120),
        )

        self.assertEqual(result.trades, ())
        self.assertEqual(result.max_position_notional_rejections, 1)
        self.assertEqual(result.positions[0].max_position_notional_rejections, 1)

    def test_applies_order_notional_before_position_limit(self) -> None:
        result = run_ma_portfolio_backtest(
            candles_by_symbol={
                "005930": _candles("005930", [100, 100, 100, 120, 130]),
            },
            quantity=Decimal(1),
            initial_cash=Decimal(1000),
            short_window=2,
            long_window=3,
            max_order_notional=Decimal(120),
            max_position_notional=Decimal(125),
        )

        self.assertEqual(result.max_order_notional_rejections, 1)
        self.assertEqual(result.max_position_notional_rejections, 0)

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

    def test_rejects_non_positive_risk_limits(self) -> None:
        candles = {"005930": _candles("005930", [10, 10, 10, 12])}
        for name, value in (
            ("max_open_positions", 0),
            ("max_daily_buys", 0),
            ("max_position_notional", Decimal(0)),
            ("max_order_notional", Decimal(0)),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, f"{name} must be positive"
            ):
                run_ma_portfolio_backtest(
                    candles_by_symbol=candles,
                    quantity=Decimal(1),
                    initial_cash=Decimal(100),
                    short_window=2,
                    long_window=3,
                    **{name: value},
                )


if __name__ == "__main__":
    unittest.main()
