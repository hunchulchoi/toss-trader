import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from toss_trader.models import Candle
from toss_trader.walk_forward import run_ma_walk_forward


def _candles(count: int) -> list[Candle]:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        Candle(
            symbol="005930",
            interval="1d",
            timestamp=started_at + timedelta(days=index),
            open_price=Decimal(100 + index),
            high_price=Decimal(100 + index),
            low_price=Decimal(100 + index),
            close_price=Decimal(100 + index),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index in range(count)
    ]


class MaWalkForwardTest(unittest.TestCase):
    def test_ranks_on_training_and_reports_validation_overfit(self) -> None:
        def fake_backtest(**kwargs: object) -> SimpleNamespace:
            short = int(kwargs["short_window"])
            candles = kwargs["candles"]
            assert isinstance(candles, list)
            validation = candles[0].timestamp.day > 10
            excess = {
                (2, False): Decimal("0.20"),
                (3, False): Decimal("0.10"),
                (2, True): Decimal("-0.05"),
                (3, True): Decimal("0.08"),
            }[(short, validation)]
            return SimpleNamespace(
                total_return_rate=excess,
                buy_hold_return_rate=Decimal(0),
                excess_return_rate=excess,
                max_drawdown_rate=Decimal("0.02"),
                completed_trades=2,
                win_rate=Decimal("0.5"),
                total_costs=Decimal(10),
            )

        with patch(
            "toss_trader.walk_forward.run_ma_backtest", side_effect=fake_backtest
        ):
            result = run_ma_walk_forward(
                candles=_candles(20),
                short_windows=(2, 3),
                long_windows=(4,),
                train_ratio=Decimal("0.6"),
                quantity=Decimal(1),
                initial_cash=Decimal(1000000),
            )

        self.assertEqual(result.train_candle_count, 12)
        self.assertEqual(result.validation_candle_count, 8)
        self.assertEqual((result.selected_short_window, result.selected_long_window), (2, 4))
        self.assertTrue(result.selected_overfit_warning)
        by_short = {candidate.short_window: candidate for candidate in result.candidates}
        self.assertEqual(by_short[2].train_rank, 1)
        self.assertEqual(by_short[2].validation_rank, 2)
        self.assertEqual(by_short[3].validation_rank, 1)

    def test_rejects_invalid_pairs_and_short_partitions(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid MA window pairs"):
            run_ma_walk_forward(
                candles=_candles(20),
                short_windows=(5,),
                long_windows=(4,),
                train_ratio=Decimal("0.6"),
                quantity=Decimal(1),
                initial_cash=Decimal(1000),
            )

    def test_warns_when_trade_sample_is_empty(self) -> None:
        empty = SimpleNamespace(
            total_return_rate=Decimal(0),
            buy_hold_return_rate=Decimal(0),
            excess_return_rate=Decimal(0),
            max_drawdown_rate=Decimal(0),
            completed_trades=0,
            win_rate=Decimal(0),
            total_costs=Decimal(0),
        )
        with patch("toss_trader.walk_forward.run_ma_backtest", return_value=empty):
            result = run_ma_walk_forward(
                candles=_candles(20),
                short_windows=(2,),
                long_windows=(4,),
                train_ratio=Decimal("0.6"),
                quantity=Decimal(1),
                initial_cash=Decimal(1000),
            )

        self.assertTrue(result.selected_overfit_warning)
        with self.assertRaisesRegex(ValueError, "each partition"):
            run_ma_walk_forward(
                candles=_candles(10),
                short_windows=(2,),
                long_windows=(4,),
                train_ratio=Decimal("0.8"),
                quantity=Decimal(1),
                initial_cash=Decimal(1000),
            )


if __name__ == "__main__":
    unittest.main()
