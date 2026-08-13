import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.backtest import run_ma_backtest
from toss_trader.models import Candle, Side


def _candles(closes: list[int], *, opens: list[int] | None = None) -> list[Candle]:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    open_prices = opens or closes
    return [
        Candle(
            symbol="005930",
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


class MovingAverageBacktestTest(unittest.TestCase):
    def test_replays_crosses_with_toss_costs(self) -> None:
        result = run_ma_backtest(
            candles=_candles(
                [10000, 10000, 10000, 12000, 12000, 12000, 8000, 7000],
                opens=[10000, 10000, 10000, 10000, 12100, 12000, 12000, 7900],
            ),
            quantity=Decimal(10),
            initial_cash=Decimal(1000000),
            short_window=2,
            long_window=3,
            slippage_rate=Decimal("0.01"),
        )

        self.assertEqual([trade.side for trade in result.trades], [Side.BUY, Side.SELL])
        self.assertEqual(result.trades[0].price, Decimal(12221))
        self.assertEqual(result.trades[1].price, Decimal(7821))
        self.assertEqual(result.trades[0].commission, Decimal(18))
        self.assertEqual(result.trades[1].commission, Decimal(11))
        self.assertEqual(result.trades[1].tax, Decimal(156))
        self.assertEqual(result.total_costs, Decimal(185))
        self.assertEqual(result.realized_pnl, Decimal(-44185))
        self.assertEqual(result.unrealized_pnl, Decimal(0))
        self.assertEqual(result.final_equity, Decimal(955815))
        self.assertEqual(result.total_return_rate, Decimal("-0.044185"))
        self.assertEqual(result.max_drawdown_rate, Decimal("0.044185"))
        self.assertEqual(result.buy_hold_return_rate, Decimal("-0.3"))
        self.assertEqual(result.excess_return_rate, Decimal("0.255815"))
        self.assertEqual(result.win_rate, Decimal(0))

    def test_marks_open_position_to_last_close(self) -> None:
        result = run_ma_backtest(
            candles=_candles([10000, 10000, 10000, 12000, 13000]),
            quantity=Decimal(10),
            initial_cash=Decimal(1000000),
            short_window=2,
            long_window=3,
        )

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.position_quantity, Decimal(10))
        self.assertEqual(result.realized_pnl, Decimal(0))
        self.assertEqual(result.trades[0].price, Decimal(13000))
        self.assertEqual(result.unrealized_pnl, Decimal(-19))
        self.assertEqual(result.final_equity, Decimal(999981))

    def test_does_not_execute_signal_on_final_candle(self) -> None:
        result = run_ma_backtest(
            candles=_candles([10000, 10000, 10000, 12000]),
            quantity=Decimal(1),
            initial_cash=Decimal(1000000),
            short_window=2,
            long_window=3,
        )

        self.assertEqual(result.trades, ())
        self.assertEqual(result.final_equity, Decimal(1000000))

    def test_rejects_unsorted_or_insufficient_candles(self) -> None:
        candles = _candles([10, 10, 10, 12])
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_ma_backtest(
                candles=list(reversed(candles)),
                quantity=Decimal(1),
                initial_cash=Decimal(100),
                short_window=2,
                long_window=3,
            )
        with self.assertRaisesRegex(ValueError, "need at least 4 candles"):
            run_ma_backtest(
                candles=candles[:3],
                quantity=Decimal(1),
                initial_cash=Decimal(100),
                short_window=2,
                long_window=3,
            )
        with self.assertRaisesRegex(ValueError, "slippage_rate"):
            run_ma_backtest(
                candles=candles,
                quantity=Decimal(1),
                initial_cash=Decimal(100),
                short_window=2,
                long_window=3,
                slippage_rate=Decimal(1),
            )


if __name__ == "__main__":
    unittest.main()
