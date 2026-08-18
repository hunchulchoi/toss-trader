import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Candle
from toss_trader.repository import SqliteMarketRepository
from toss_trader.screening import MarketRegime
from toss_trader.setup_screening import SetupDecision, SetupType, ValuationTier
from toss_trader.v2_engine import DailySetupCandidate
from toss_trader.v2_screening import V2MarketScanner, v2_market_scan_to_dict


def _candles(symbol: str, count: int) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            symbol=symbol,
            interval="1d",
            timestamp=start + timedelta(days=index),
            open_price=Decimal(100 + index),
            high_price=Decimal(101 + index),
            low_price=Decimal(99 + index),
            close_price=Decimal(100 + index),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index in range(count)
    ]


class FakeCollector:
    def __init__(self, repository: SqliteMarketRepository) -> None:
        self.repository = repository
        self.counts: dict[str, int] = {}

    def collect_symbol_names(self, symbols: tuple[str, ...]) -> dict[str, str]:
        return {symbol: f"Name {symbol}" for symbol in symbols}

    def collect(self, *, symbol: str, interval: str, count: int, **kwargs):
        del kwargs
        self.counts[symbol] = count
        candles = _candles(symbol, count)
        self.repository.upsert_candles(candles)


class FakeBuilder:
    def build_candidate(self, symbol: str, *, now: datetime) -> DailySetupCandidate:
        del now
        if symbol == "000660":
            raise ValueError("setup-v2:missing:completed-daily-candles(199/200)")
        decision = SetupDecision(
            symbol=symbol,
            approved=True,
            setups=(SetupType.PULLBACK, SetupType.FLOW_REVERSAL),
            violations=(),
            missing_checks=(),
            rsi14=Decimal(50),
            ma50=Decimal(280),
            ma200=Decimal(200),
            ma50_distance=Decimal("0.01"),
            flow_stars=2,
            flow_summary=None,
            valuation_tier=ValuationTier.B,
            confidence_multiplier=Decimal(1),
            proposed_confidence_multiplier=Decimal(1),
        )
        return DailySetupCandidate(
            symbol=symbol,
            signal_session=datetime(2026, 8, 17, tzinfo=UTC).date(),
            close_price=Decimal(299),
            setup_low=Decimal(295),
            ma50=Decimal(280),
            atr14=Decimal(3),
            decision=decision,
        )


class V2MarketScannerTest(unittest.TestCase):
    def test_uses_v2_candidates_and_summarizes_readiness(self) -> None:
        repository = SqliteMarketRepository(":memory:")
        try:
            collector = FakeCollector(repository)
            result = V2MarketScanner(
                collector=collector,
                repository=repository,
                candidate_builder=FakeBuilder(),
            ).run(
                benchmark_symbols=("069500",),
                discovery_symbols=("005930", "000660"),
                top_n=10,
                now=datetime(2026, 8, 18, 8, 30, tzinfo=UTC),
            )
            payload = v2_market_scan_to_dict(result)

            self.assertEqual(result.markets[0].regime, MarketRegime.RISK_ON)
            self.assertEqual(collector.counts["069500"], 60)
            self.assertEqual(collector.counts["005930"], 200)
            self.assertEqual(payload["entryStrategy"], "setup-v2.2-independent-daily")
            self.assertEqual(payload["candidateSummary"]["scanned"], 2)
            self.assertEqual(payload["candidateSummary"]["evaluated"], 1)
            self.assertEqual(payload["candidateSummary"]["approved"], 1)
            self.assertEqual(payload["candidateSummary"]["blocked"], 1)
            self.assertEqual(
                payload["blockedReasons"],
                {"missing:completed-daily-candles(199/200)": 1},
            )
            self.assertEqual(payload["candidates"][0]["flowStars"], 2)
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
