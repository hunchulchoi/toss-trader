import unittest
from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.models import Side
from toss_trader.strategy import (
    evaluate_ma_crossover,
    ma_crossover_signal,
    ma_trend_continuation_signal,
)


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

    def test_evaluation_reports_no_crossover_and_ma_relation(self) -> None:
        result = evaluate_ma_crossover(
            symbol="AAPL",
            closes=[Decimal(10), Decimal(11), Decimal(12), Decimal(13)],
            as_of=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            quantity=Decimal(1),
            short_window=2,
            long_window=3,
        )

        self.assertIsNone(result.signal)
        self.assertEqual(result.close, Decimal(13))
        self.assertEqual(result.short_ma, Decimal("12.5"))
        self.assertEqual(result.long_ma, Decimal(12))
        self.assertEqual(result.relation, "above")

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

    def test_continuation_requires_close_above_short_above_long(self) -> None:
        aligned = evaluate_ma_crossover(
            symbol="AAPL",
            closes=[Decimal(10), Decimal(11), Decimal(12), Decimal(13)],
            as_of=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            quantity=Decimal(1),
            short_window=2,
            long_window=3,
        )
        pulled_back = evaluate_ma_crossover(
            symbol="AAPL",
            closes=[Decimal(10), Decimal(11), Decimal(12), Decimal(11)],
            as_of=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
            quantity=Decimal(1),
            short_window=2,
            long_window=3,
        )

        signal = ma_trend_continuation_signal(
            evaluation=aligned,
            symbol="AAPL",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            entry_key="2026-08-12",
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, Side.BUY)
        self.assertEqual(signal.reason, "MA2/MA3 trend continuation")
        self.assertTrue(signal.signal_id.endswith("cont-2026-08-12"))
        self.assertIsNone(
            ma_trend_continuation_signal(
                evaluation=pulled_back,
                symbol="AAPL",
                short_window=2,
                long_window=3,
                quantity=Decimal(1),
                entry_key="2026-08-12",
            )
        )


if __name__ == "__main__":
    unittest.main()
