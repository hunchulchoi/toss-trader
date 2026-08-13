import unittest
from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.models import Side
from toss_trader.strategy import ma_crossover_signal


class MovingAverageStrategyTest(unittest.TestCase):
    def test_emits_buy_only_on_new_golden_cross(self) -> None:
        result = ma_crossover_signal(
            symbol="005930",
            closes=[Decimal(10), Decimal(10), Decimal(10), Decimal(12)],
            as_of=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            quantity=Decimal(1),
            short_window=2,
            long_window=3,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.side, Side.BUY)
        self.assertEqual(result.reference_price, Decimal(12))

    def test_emits_nothing_without_cross(self) -> None:
        result = ma_crossover_signal(
            symbol="AAPL",
            closes=[Decimal(10), Decimal(11), Decimal(12), Decimal(13)],
            as_of=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            quantity=Decimal(1),
            short_window=2,
            long_window=3,
        )

        self.assertIsNone(result)

    def test_emits_entry_for_existing_uptrend_when_explicitly_allowed(self) -> None:
        result = ma_crossover_signal(
            symbol="AAPL",
            closes=[Decimal(10), Decimal(11), Decimal(12), Decimal(13)],
            as_of=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            quantity=Decimal(1),
            short_window=2,
            long_window=3,
            allow_trend_entry=True,
            entry_key="universe-run-1",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.side, Side.BUY)
        self.assertEqual(result.reason, "MA2/MA3 trend entry")
        self.assertTrue(result.signal_id.endswith("universe-run-1"))


if __name__ == "__main__":
    unittest.main()
