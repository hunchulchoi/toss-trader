import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Candle
from toss_trader.repository import SqliteMarketRepository
from toss_trader.risk import RiskLimits, RiskManager, UniverseRiskContext
from toss_trader.universe import DynamicUniverseSelector, SqliteUniverseStore

NOW = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)


def daily_history(symbol: str, *, price_setup: bool) -> list[Candle]:
    closes = ([100] * 150 + [120] * 49 + [121]) if price_setup else [200] * 200
    started_at = NOW - timedelta(days=200)
    return [
        Candle(
            symbol=symbol,
            interval="1d",
            timestamp=started_at + timedelta(days=index),
            open_price=Decimal(close),
            high_price=Decimal(close + 1),
            low_price=Decimal(close - 1),
            close_price=Decimal(close),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index, close in enumerate(closes)
    ]


class FakeDailyCollector:
    def collect(self, **_kwargs: object):
        raise AssertionError("stored 200-day history should be reused")


class FakeRankingClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ranking_calls: list[str] = []

    def rankings(self, *, ranking_type: str, **kwargs: object) -> dict:
        del kwargs
        self.ranking_calls.append(ranking_type)
        if self.fail:
            raise RuntimeError("ranking unavailable")
        symbols = (
            ("005930", "71000", "1000000000", "0.02"),
            ("000660", "190000", "900000000", "0.03"),
            ("207940", "1500000", "800000000", "0.04"),
        )
        rows = [
            {
                "rank": index,
                "symbol": symbol,
                "price": {"lastPrice": price, "changeRate": change},
                "tradingAmount": amount,
            }
            for index, (symbol, price, amount, change) in enumerate(symbols, start=1)
        ]
        if ranking_type == "TOP_GAINERS":
            rows.reverse()
            for index, row in enumerate(rows, start=1):
                row["rank"] = index
        return {"rankedAt": NOW.isoformat(), "rankings": rows}

    def stocks(self, symbols: tuple[str, ...]) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "name": f"Name {symbol}",
                "securityType": "STOCK",
                "isCommonShare": True,
                "status": "ACTIVE",
                "koreanMarketDetail": {
                    "krxTradingSuspended": False,
                    "nxtTradingSuspended": False,
                },
            }
            for symbol in symbols
        ]


class DynamicUniverseSelectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SqliteMarketRepository(":memory:")
        self.store = SqliteUniverseStore(":memory:")
        self.repository.upsert_candles(daily_history("005930", price_setup=True))
        self.repository.upsert_candles(daily_history("000660", price_setup=False))
        self.repository.upsert_candles(daily_history("207940", price_setup=True))

    def tearDown(self) -> None:
        self.store.close()
        self.repository.close()

    def _selector(self, client: FakeRankingClient) -> DynamicUniverseSelector:
        return DynamicUniverseSelector(
            client=client,
            collector=FakeDailyCollector(),
            repository=self.repository,
            store=self.store,
            risk_manager=RiskManager(RiskLimits()),
            candidate_count=3,
            universe_size=2,
        )

    def test_refreshes_risk_approved_universe_and_reuses_cache(self) -> None:
        client = FakeRankingClient()
        context = UniverseRiskContext(
            quantity=Decimal(1),
            available_cash=Decimal(1000000),
            daily_return_rate=Decimal(0),
            consecutive_api_errors=0,
        )

        first = self._selector(client).resolve(
            now=NOW, held_symbols=(), risk_context=context
        )
        second = self._selector(client).resolve(
            now=NOW + timedelta(hours=2),
            held_symbols=("035420",),
            risk_context=context,
        )
        third = self._selector(client).resolve(
            now=NOW + timedelta(days=1),
            held_symbols=(),
            risk_context=context,
        )

        self.assertTrue(first.refreshed)
        self.assertTrue(first.new_buys_allowed)
        self.assertEqual(first.symbols, ("005930",))
        self.assertEqual(first.entry_symbols, ("005930",))
        self.assertFalse(second.refreshed)
        self.assertEqual(second.entry_symbols, ())
        self.assertEqual(second.symbols, ("005930", "035420"))
        self.assertTrue(third.refreshed)
        self.assertEqual(third.symbols, ("005930",))
        self.assertEqual(len(client.ranking_calls), 4)
        rows = self.store._connection.execute(
            """
            SELECT symbol, risk_approved, selected, violations
            FROM dynamic_universe_decisions ORDER BY score DESC, symbol
            """
        ).fetchall()
        self.assertEqual(rows[0][0], "005930")
        expensive = next(row for row in rows if row[0] == "207940")
        self.assertEqual(expensive[1:3], (0, 0))
        self.assertIn("max-order-notional", expensive[3])
        no_setup = next(row for row in rows if row[0] == "000660")
        self.assertEqual(no_setup[1:3], (0, 0))
        self.assertIn("missing-price-setup", no_setup[3])

    def test_empty_price_setup_pool_is_successful_without_fillers(self) -> None:
        self.repository.upsert_candles(daily_history("005930", price_setup=False))
        self.repository.upsert_candles(daily_history("207940", price_setup=False))
        client = FakeRankingClient()

        result = self._selector(client).resolve(
            now=NOW,
            held_symbols=(),
            risk_context=UniverseRiskContext(
                quantity=Decimal(1),
                available_cash=Decimal(1000000),
                daily_return_rate=Decimal(0),
                consecutive_api_errors=0,
            ),
        )

        self.assertEqual(result.symbols, ())
        self.assertTrue(result.new_buys_allowed)
        row = self.store._connection.execute(
            "SELECT status, selected_count FROM dynamic_universe_runs"
        ).fetchone()
        self.assertEqual(row, ("succeeded", 0))
        cached = self._selector(client).resolve(
            now=NOW + timedelta(hours=3),
            held_symbols=(),
            risk_context=UniverseRiskContext(
                quantity=Decimal(1),
                available_cash=Decimal(1000000),
                daily_return_rate=Decimal(0),
                consecutive_api_errors=0,
            ),
        )
        self.assertFalse(cached.refreshed)
        self.assertEqual(cached.symbols, ())
        self.assertEqual(len(client.ranking_calls), 2)

    def test_ranking_failure_tracks_held_symbols_only(self) -> None:
        result = self._selector(FakeRankingClient(fail=True)).resolve(
            now=NOW,
            held_symbols=("005930",),
            risk_context=UniverseRiskContext(
                quantity=Decimal(1),
                available_cash=Decimal(1000000),
                daily_return_rate=Decimal(0),
                consecutive_api_errors=0,
            ),
        )

        self.assertEqual(result.symbols, ("005930",))
        self.assertFalse(result.new_buys_allowed)
        row = self.store._connection.execute(
            "SELECT status, error_message FROM dynamic_universe_runs"
        ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("ranking unavailable", row[1])


if __name__ == "__main__":
    unittest.main()
