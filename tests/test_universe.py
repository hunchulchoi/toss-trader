import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from toss_trader.models import Candle
from toss_trader.repository import SqliteMarketRepository
from toss_trader.risk import RiskDecision, RiskLimits, RiskManager, UniverseRiskContext
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
    def __init__(self, *, fail_symbols: tuple[str, ...] = ()) -> None:
        self.fail_symbols = set(fail_symbols)
        self.calls: list[str] = []

    def collect(self, *, symbol: str, **_kwargs: object) -> SimpleNamespace:
        self.calls.append(symbol)
        if symbol in self.fail_symbols:
            raise RuntimeError(f"daily unavailable for {symbol}")
        return SimpleNamespace(received=0, upserted=0, next_before=None)


class ExhaustedHistoryCollector:
    def __init__(
        self, repository: SqliteMarketRepository, candles: list[Candle]
    ) -> None:
        self.repository = repository
        self.candles = candles

    def collect(self, **_kwargs: object) -> SimpleNamespace:
        upserted = self.repository.upsert_candles(self.candles)
        return SimpleNamespace(
            received=len(self.candles), upserted=upserted, next_before=None
        )


DEFAULT_ROWS = (
    ("005930", "71000", "1000000000", "0.02"),
    ("000660", "190000", "900000000", "0.03"),
    ("207940", "1500000", "800000000", "0.04"),
)


class FakeRankingClient:
    def __init__(
        self,
        *,
        fail: bool = False,
        rows: tuple[tuple[str, str, str, str], ...] = DEFAULT_ROWS,
        stock_overrides: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.fail = fail
        self.rows = rows
        self.stock_overrides = stock_overrides or {}
        self.ranking_calls: list[tuple[str, int]] = []

    def rankings(
        self, *, ranking_type: str, count: int, **_kwargs: object
    ) -> dict:
        self.ranking_calls.append((ranking_type, count))
        if self.fail:
            raise RuntimeError("ranking unavailable")
        rows = [
            {
                "rank": index,
                "symbol": symbol,
                "price": {"lastPrice": price, "changeRate": change},
                "tradingAmount": amount,
            }
            for index, (symbol, price, amount, change) in enumerate(
                self.rows, start=1
            )
        ]
        return {"rankedAt": NOW.isoformat(), "rankings": rows}

    def stocks(self, symbols: tuple[str, ...]) -> list[dict]:
        result = []
        for symbol in symbols:
            stock: dict[str, object] = {
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
            stock.update(self.stock_overrides.get(symbol, {}))
            result.append(stock)
        return result


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

    def _selector(
        self,
        client: FakeRankingClient,
        *,
        collector: object | None = None,
        candidate_count: int = 3,
        fetch_count: int = 5,
        universe_size: int = 2,
        risk_manager: object | None = None,
    ) -> DynamicUniverseSelector:
        return DynamicUniverseSelector(
            client=client,
            collector=collector or FakeDailyCollector(),
            repository=self.repository,
            store=self.store,
            risk_manager=risk_manager or RiskManager(RiskLimits()),  # type: ignore[arg-type]
            candidate_count=candidate_count,
            ranking_fetch_count=fetch_count,
            universe_size=universe_size,
        )

    @staticmethod
    def _context() -> UniverseRiskContext:
        return UniverseRiskContext(
            quantity=Decimal(100),
            available_cash=Decimal(0),
            daily_return_rate=Decimal("-0.50"),
            consecutive_api_errors=100,
        )

    def test_amount_only_price_setup_universe_reuses_daily_cache(self) -> None:
        client = FakeRankingClient()

        first = self._selector(client).resolve(
            now=NOW, held_symbols=(), risk_context=self._context()
        )
        second = self._selector(client).resolve(
            now=NOW + timedelta(hours=2),
            held_symbols=("035420",),
            risk_context=self._context(),
        )
        third = self._selector(client).resolve(
            now=NOW + timedelta(days=1),
            held_symbols=(),
            risk_context=self._context(),
        )

        self.assertEqual(first.symbols, ("005930", "000660"))
        self.assertEqual(first.entry_symbols, ("005930", "000660"))
        self.assertFalse(second.refreshed)
        self.assertEqual(second.symbols, ("005930", "000660", "035420"))
        self.assertTrue(third.refreshed)
        self.assertEqual(client.ranking_calls, [("MARKET_TRADING_AMOUNT", 5)] * 2)
        rows = self.store._connection.execute(
            """
            SELECT symbol, amount_rank, eligible_rank, risk_approved, selected,
                   violations
            FROM dynamic_universe_decisions ORDER BY amount_rank, symbol
            """
        ).fetchall()
        no_setup = next(row for row in rows if row[0] == "000660")
        self.assertEqual(no_setup[1:5], (2, 2, 1, 1))
        self.assertIn("missing-price-setup", no_setup[5])

    def test_overfetches_then_filters_and_reranks_static_eligible_stocks(self) -> None:
        rows = (
            ("069500", "40000", "1200000000", "0.01"),
            ("005935", "60000", "1100000000", "0.01"),
            ("000001", "50000", "1050000000", "0.01"),
            ("005930", "71000", "1000000000", "0.02"),
            ("207940", "1500000", "800000000", "0.04"),
        )
        client = FakeRankingClient(
            rows=rows,
            stock_overrides={
                "069500": {"securityType": "ETF", "isCommonShare": False},
                "005935": {"isCommonShare": False},
                "000001": {
                    "koreanMarketDetail": {
                        "krxTradingSuspended": True,
                        "nxtTradingSuspended": False,
                    }
                },
            },
        )
        collector = FakeDailyCollector()

        result = self._selector(
            client,
            collector=collector,
            candidate_count=2,
            fetch_count=5,
            universe_size=2,
        ).resolve(now=NOW, held_symbols=(), risk_context=self._context())

        self.assertEqual(result.symbols, ("005930", "207940"))
        self.assertEqual(collector.calls, [])
        rows = self.store._connection.execute(
            """
            SELECT symbol, amount_rank, eligible_rank, selected, violations
            FROM dynamic_universe_decisions ORDER BY amount_rank
            """
        ).fetchall()
        self.assertEqual(rows[3][0:4], ("005930", 4, 1, 1))
        self.assertEqual(rows[4][0:4], ("207940", 5, 2, 1))
        self.assertIn("unsupported-security-type", rows[0][4])
        self.assertIn("not-common-share", rows[1][4])
        self.assertIn("trading-suspended", rows[2][4])

    def test_top_gainers_never_participates_in_authoritative_selection(self) -> None:
        client = FakeRankingClient()

        self._selector(client).resolve(
            now=NOW, held_symbols=(), risk_context=self._context()
        )

        self.assertEqual(client.ranking_calls, [("MARKET_TRADING_AMOUNT", 5)])

    def test_provider_cap_shortfall_succeeds_without_filler(self) -> None:
        client = FakeRankingClient(
            rows=(
                ("005930", "71000", "100", "0.01"),
                ("207940", "1500000", "90", "0.01"),
            )
        )

        result = self._selector(
            client, candidate_count=3, fetch_count=5, universe_size=3
        ).resolve(now=NOW, held_symbols=(), risk_context=self._context())

        self.assertEqual(result.symbols, ("005930", "207940"))
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status, selected_count FROM dynamic_universe_runs"
            ).fetchone(),
            ("succeeded", 2),
        )

    def test_unexpected_remote_risk_violation_is_data_error(self) -> None:
        class StaleRiskManager:
            def evaluate_universe_candidate(self, *_args: object) -> RiskDecision:
                return RiskDecision(False, ("max-order-notional",))

        with self.assertRaisesRegex(RuntimeError, "risk policy unavailable"):
            self._selector(
                FakeRankingClient(), risk_manager=StaleRiskManager()
            ).resolve(now=NOW, held_symbols=(), risk_context=self._context())

        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM dynamic_universe_runs"
            ).fetchone(),
            ("failed",),
        )

    def test_naive_ranked_at_is_data_error(self) -> None:
        class NaiveRankedAtClient(FakeRankingClient):
            def rankings(self, **kwargs: object) -> dict:
                payload = super().rankings(**kwargs)
                payload["rankedAt"] = "2026-08-13T09:00:00"
                return payload

        with self.assertRaisesRegex(ValueError, "timezone offset"):
            self._selector(NaiveRankedAtClient()).resolve(
                now=NOW, held_symbols=(), risk_context=self._context()
            )

    def test_malformed_stock_metadata_fails_and_retries_same_day(self) -> None:
        client = FakeRankingClient(
            rows=(("005930", "71000", "100", "0.01"),),
            stock_overrides={"005930": {"koreanMarketDetail": []}},
        )
        selector = self._selector(
            client, candidate_count=1, fetch_count=1, universe_size=1
        )

        with self.assertRaisesRegex(TypeError, "koreanMarketDetail"):
            selector.resolve(now=NOW, held_symbols=(), risk_context=self._context())
        client.stock_overrides = {}
        recovered = selector.resolve(
            now=NOW + timedelta(minutes=1),
            held_symbols=(),
            risk_context=self._context(),
        )

        self.assertEqual(recovered.symbols, ("005930",))
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM dynamic_universe_runs ORDER BY evaluated_at"
            ).fetchall(),
            [("failed",), ("succeeded",)],
        )

    def test_missing_ranking_number_is_data_error_not_static_rejection(self) -> None:
        class MissingPriceClient(FakeRankingClient):
            def rankings(self, **kwargs: object) -> dict:
                payload = super().rankings(**kwargs)
                del payload["rankings"][0]["price"]["lastPrice"]
                return payload

        with self.assertRaisesRegex(ValueError, "missing lastPrice"):
            self._selector(MissingPriceClient()).resolve(
                now=NOW, held_symbols=(), risk_context=self._context()
            )

        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM dynamic_universe_runs"
            ).fetchone(),
            ("failed",),
        )

    def test_data_error_fails_without_freeze_then_retries_same_day(self) -> None:
        client = FakeRankingClient(rows=(("035420", "50000", "100", "0.01"),))
        collector = FakeDailyCollector(fail_symbols=("035420",))
        selector = self._selector(
            client,
            collector=collector,
            candidate_count=1,
            fetch_count=1,
            universe_size=1,
        )

        failed = selector.resolve(
            now=NOW, held_symbols=("005930",), risk_context=self._context()
        )
        self.assertEqual(failed.symbols, ("005930",))
        self.assertFalse(failed.new_buys_allowed)
        self.repository.upsert_candles(daily_history("035420", price_setup=True))
        recovered = selector.resolve(
            now=NOW + timedelta(minutes=5),
            held_symbols=(),
            risk_context=self._context(),
        )
        cached = selector.resolve(
            now=NOW + timedelta(minutes=10),
            held_symbols=(),
            risk_context=self._context(),
        )

        self.assertEqual(recovered.symbols, ("035420",))
        self.assertFalse(cached.refreshed)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM dynamic_universe_runs ORDER BY evaluated_at"
            ).fetchall(),
            [("failed",), ("succeeded",)],
        )
        self.assertEqual(len(client.ranking_calls), 2)

    def test_no_price_setup_still_selects_eligible_and_freezes(self) -> None:
        self.repository.upsert_candles(daily_history("005930", price_setup=False))
        self.repository.upsert_candles(daily_history("207940", price_setup=False))
        client = FakeRankingClient()
        selector = self._selector(client)

        result = selector.resolve(
            now=NOW, held_symbols=(), risk_context=self._context()
        )
        cached = selector.resolve(
            now=NOW + timedelta(hours=3),
            held_symbols=(),
            risk_context=self._context(),
        )

        self.assertEqual(result.symbols, ("005930", "000660"))
        self.assertFalse(cached.refreshed)
        self.assertEqual(client.ranking_calls, [("MARKET_TRADING_AMOUNT", 5)])
        row = self.store._connection.execute(
            "SELECT status, selected_count FROM dynamic_universe_runs"
        ).fetchone()
        self.assertEqual(row, ("succeeded", 2))

    def test_insufficient_history_is_normal_rejection_not_data_error(self) -> None:
        history = daily_history("035420", price_setup=True)[:199]
        client = FakeRankingClient(rows=(("035420", "50000", "100", "0.01"),))

        result = self._selector(
            client,
            collector=ExhaustedHistoryCollector(self.repository, history),
            candidate_count=1,
            fetch_count=1,
            universe_size=1,
        ).resolve(now=NOW, held_symbols=(), risk_context=self._context())

        self.assertEqual(result.symbols, ())
        row = self.store._connection.execute(
            "SELECT status, selected_count FROM dynamic_universe_runs"
        ).fetchone()
        decision = self.store._connection.execute(
            "SELECT violations FROM dynamic_universe_decisions"
        ).fetchone()
        self.assertEqual(row, ("succeeded", 0))
        self.assertIn("completed-daily-candles(199/200)", decision[0])
        self.assertIsNone(
            self.store.latest_selected_between(NOW, NOW + timedelta(minutes=5))
        )

    def test_partial_history_with_empty_response_is_data_error(self) -> None:
        self.repository.upsert_candles(daily_history("035420", price_setup=True)[:199])
        client = FakeRankingClient(rows=(("035420", "50000", "100", "0.01"),))

        with self.assertRaisesRegex(RuntimeError, "price data unavailable"):
            self._selector(
                client,
                candidate_count=1,
                fetch_count=1,
                universe_size=1,
            ).resolve(now=NOW, held_symbols=(), risk_context=self._context())

        self.assertEqual(
            self.store._connection.execute(
                "SELECT status FROM dynamic_universe_runs"
            ).fetchone(),
            ("failed",),
        )

    def test_ranking_failure_tracks_held_symbols_only(self) -> None:
        result = self._selector(FakeRankingClient(fail=True)).resolve(
            now=NOW,
            held_symbols=("005930",),
            risk_context=self._context(),
        )

        self.assertEqual(result.symbols, ("005930",))
        self.assertFalse(result.new_buys_allowed)
        row = self.store._connection.execute(
            "SELECT status, error_message FROM dynamic_universe_runs"
        ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("ranking unavailable", row[1])

    def test_existing_sqlite_store_adds_eligible_rank_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            connection = sqlite3.connect(database_path)
            connection.execute(
                """
                CREATE TABLE dynamic_universe_runs (
                    run_id TEXT PRIMARY KEY,
                    evaluated_at TEXT NOT NULL,
                    ranked_at TEXT,
                    status TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    approved_count INTEGER NOT NULL,
                    selected_count INTEGER NOT NULL,
                    error_message TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE dynamic_universe_decisions (
                    decision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    score TEXT NOT NULL,
                    amount_rank INTEGER,
                    gainer_rank INTEGER,
                    change_rate TEXT NOT NULL,
                    trading_amount TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    risk_approved INTEGER NOT NULL,
                    selected INTEGER NOT NULL,
                    violations TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """INSERT INTO dynamic_universe_runs VALUES
                ('legacy', ?, NULL, 'succeeded', 3, 3, 3, NULL)""",
                (NOW.isoformat(),),
            )
            connection.executemany(
                """INSERT INTO dynamic_universe_decisions VALUES
                (?, 'legacy', ?, ?, ?, ?, ?, '0', '1', '1', 1, 1, '[]')""",
                (
                    ("d1", NOW.isoformat(), "LOW", "10", 1, None),
                    ("d2", NOW.isoformat(), "GAINER", "30", None, 1),
                    ("d3", NOW.isoformat(), "MID", "20", 2, None),
                ),
            )
            connection.commit()
            connection.close()

            for _ in range(2):
                store = SqliteUniverseStore(database_path)
                columns = {
                    row[1]
                    for row in store._connection.execute(
                        "PRAGMA table_info(dynamic_universe_decisions)"
                    )
                }
                self.assertIn("eligible_rank", columns)
                self.assertEqual(
                    store.latest_selected_between(
                        NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)
                    ),
                    ("GAINER", "MID", "LOW"),
                )
                store.close()


if __name__ == "__main__":
    unittest.main()
