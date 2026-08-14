import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.exit_counterfactual import (
    ExitPolicy,
    ExitVariant,
    run_exit_counterfactual_matrix,
)
from toss_trader.models import Candle


def candles(
    closes: list[int],
    *,
    opens: list[int] | None = None,
    highs: list[int] | None = None,
    lows: list[int] | None = None,
) -> list[Candle]:
    open_values = opens or closes
    high_values = highs or [max(open_, close) for open_, close in zip(open_values, closes)]
    low_values = lows or [min(open_, close) for open_, close in zip(open_values, closes)]
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            symbol="005930",
            interval="1m",
            timestamp=started_at + timedelta(minutes=index),
            open_price=Decimal(open_values[index]),
            high_price=Decimal(high_values[index]),
            low_price=Decimal(low_values[index]),
            close_price=Decimal(close),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index, close in enumerate(closes)
    ]


class ExitCounterfactualTest(unittest.TestCase):
    def test_dead_cross_executes_at_next_open(self) -> None:
        source = candles(
            [10000, 10000, 10000, 12000, 12000, 8000, 7000],
            opens=[10000, 10000, 10000, 11000, 12100, 12000, 7900],
        )

        result = run_exit_counterfactual_matrix(
            candles_by_symbol={"005930": source},
            variants=(ExitVariant("dead", ExitPolicy.DEAD_CROSS),),
            short_window=2,
            long_window=3,
            slippage_rate=Decimal("0.01"),
        )[0]

        self.assertEqual(result.entry_count, 1)
        self.assertEqual(result.completed_trades, 1)
        trade = result.trades[0]
        self.assertEqual(trade.entered_at, source[4].timestamp)
        self.assertEqual(trade.exited_at, source[6].timestamp)
        self.assertEqual(trade.entry_price, Decimal(12221))
        self.assertEqual(trade.exit_price, Decimal(7821))
        self.assertEqual(trade.held_bars, 2)
        self.assertEqual(trade.exit_reason, "dead-cross")

    def test_min_hold_uses_below_state_after_hold_expires(self) -> None:
        source = candles(
            [100, 100, 100, 120, 120, 80, 80, 80],
            opens=[100, 100, 100, 110, 121, 120, 80, 70],
        )

        result = run_exit_counterfactual_matrix(
            candles_by_symbol={"005930": source},
            variants=(
                ExitVariant("hold-3", ExitPolicy.MIN_HOLD, min_hold_bars=3),
            ),
            short_window=2,
            long_window=3,
            slippage_rate=Decimal(0),
        )[0]

        trade = result.trades[0]
        self.assertEqual(trade.entered_at, source[4].timestamp)
        self.assertEqual(trade.exited_at, source[7].timestamp)
        self.assertEqual(trade.held_bars, 3)
        self.assertEqual(trade.exit_reason, "min-hold-dead-cross")

    def test_atr_stop_uses_prior_stop_and_gap_open(self) -> None:
        source = candles(
            [100, 100, 100, 110, 110, 90],
            opens=[100, 100, 100, 105, 110, 80],
            highs=[101, 101, 101, 112, 120, 95],
            lows=[99, 99, 99, 104, 108, 75],
        )

        result = run_exit_counterfactual_matrix(
            candles_by_symbol={"005930": source},
            variants=(
                ExitVariant("dead", ExitPolicy.DEAD_CROSS),
                ExitVariant(
                    "atr",
                    ExitPolicy.ATR_TRAILING,
                    atr_period=2,
                    atr_multiple=Decimal(2),
                ),
            ),
            short_window=2,
            long_window=3,
            slippage_rate=Decimal(0),
        )[1]

        trade = result.trades[0]
        self.assertEqual(trade.entered_at, source[4].timestamp)
        self.assertEqual(trade.exited_at, source[5].timestamp)
        self.assertEqual(trade.exit_price, Decimal(80))
        self.assertEqual(trade.exit_reason, "atr-stop")

    def test_final_candle_signal_is_not_executed(self) -> None:
        result = run_exit_counterfactual_matrix(
            candles_by_symbol={"005930": candles([100, 100, 100, 120])},
            variants=(ExitVariant("dead", ExitPolicy.DEAD_CROSS),),
            short_window=2,
            long_window=3,
        )[0]

        self.assertEqual(result.entry_count, 0)

    def test_rejects_unsorted_or_non_minute_input(self) -> None:
        source = candles([100, 100, 100, 120])
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_exit_counterfactual_matrix(
                candles_by_symbol={"005930": list(reversed(source))},
                variants=(ExitVariant("dead", ExitPolicy.DEAD_CROSS),),
                short_window=2,
                long_window=3,
            )
        source[0] = Candle(
            symbol="005930",
            interval="1d",
            timestamp=source[0].timestamp,
            open_price=source[0].open_price,
            high_price=source[0].high_price,
            low_price=source[0].low_price,
            close_price=source[0].close_price,
            volume=source[0].volume,
            currency=source[0].currency,
        )
        with self.assertRaisesRegex(ValueError, "use 1m"):
            run_exit_counterfactual_matrix(
                candles_by_symbol={"005930": source},
                variants=(ExitVariant("dead", ExitPolicy.DEAD_CROSS),),
                short_window=2,
                long_window=3,
            )


if __name__ == "__main__":
    unittest.main()
