import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Candle
from toss_trader.repository import SqliteMarketRepository
from toss_trader.setup_screening import SetupContext
from toss_trader.v2_runtime import OfficialV2CycleStrategy


def candle(symbol: str, interval: str, timestamp: datetime, price: int) -> Candle:
    value = Decimal(price)
    return Candle(
        symbol=symbol,
        interval=interval,
        timestamp=timestamp,
        open_price=value,
        high_price=value + 1,
        low_price=value - 1,
        close_price=value,
        volume=Decimal(1000),
        currency="KRW",
    )


class OfficialV2CycleStrategyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SqliteMarketRepository(":memory:")
        self.contexts: list[tuple] = []

        def context_factory(symbol, signal_session, decision_at, gap_up):
            self.contexts.append((symbol, signal_session, decision_at, gap_up))
            return SetupContext(
                decision_at=decision_at,
                signal_session=signal_session,
                event_imminent=None,
                gap_up_chase=gap_up,
            )

        self.strategy = OfficialV2CycleStrategy(
            self.repository, context_factory=context_factory
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_excludes_current_session_daily_bar(self) -> None:
        start = datetime(2025, 12, 1, tzinfo=UTC)
        history = [
            candle("005930", "1d", start + timedelta(days=index), 100 + index)
            for index in range(201)
        ]
        self.repository.upsert_candles(history)
        now = history[-1].timestamp + timedelta(hours=12)

        candidate = self.strategy.build_candidate("005930", now=now)

        self.assertEqual(candidate.close_price, history[-2].close_price)
        self.assertEqual(self.contexts[-1][3], False)
        daily = self.strategy.completed_daily_bars("005930", now=now, limit=30)
        self.assertEqual(len(daily), 30)
        self.assertEqual(daily[-1].close_price, history[-2].close_price)

    def test_requires_two_hundred_completed_daily_bars(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.repository.upsert_candles(
            [
                candle("005930", "1d", start + timedelta(days=index), 100)
                for index in range(199)
            ]
        )
        now = start + timedelta(days=200)

        with self.assertRaisesRegex(ValueError, "completed-daily-candles"):
            self.strategy.build_candidate("005930", now=now)

    def test_returns_only_completed_one_minute_bars(self) -> None:
        now = datetime(2026, 8, 18, 0, 5, 30, tzinfo=UTC)
        self.repository.upsert_candles(
            [
                candle("005930", "1m", now - timedelta(minutes=2), 100),
                candle("005930", "1m", now, 101),
                candle("005930", "1m", now + timedelta(minutes=1), 102),
            ]
        )

        bars = self.strategy.completed_one_minute_bars("005930", now=now)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1].close_price, Decimal(101))

    def test_unknown_symbols_share_conservative_cluster(self) -> None:
        self.assertEqual(self.strategy.cluster_id("005930"), "UNKNOWN")
        self.assertEqual(self.strategy.cluster_id("005380"), "UNKNOWN")

    def test_mapped_symbol_returns_cluster(self) -> None:
        self.repository.upsert_symbol_clusters({"005930": "전기전자", "005380": "운수장비"})
        self.assertEqual(self.strategy.cluster_id("005930"), "전기전자")
        self.assertEqual(self.strategy.cluster_id("005380"), "운수장비")
        self.assertEqual(self.strategy.cluster_id("999999"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
