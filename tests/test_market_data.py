import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Self

from toss_trader.market_data import MarketCollector, StoredMaStrategy
from toss_trader.models import Candle, Side
from toss_trader.repository import (
    PostgresMarketReadRepository,
    PostgresMarketRepository,
    SqliteMarketReadRepository,
    SqliteMarketRepository,
)


class FakeCandleClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def candles(self, symbol: str, **kwargs: object) -> dict:
        self.calls.append({"symbol": symbol, **kwargs})
        return self.payload

    def stocks(self, symbols: tuple[str, ...]) -> list[dict]:
        return [{"symbol": symbol, "name": f"Name {symbol}"} for symbol in symbols]


def candle_payload() -> dict:
    return {
        "candles": [
            {
                "timestamp": "2026-08-12T09:01:00+09:00",
                "openPrice": "71000",
                "highPrice": "71200",
                "lowPrice": "70900",
                "closePrice": "71100",
                "volume": "1200",
                "currency": "KRW",
            },
            {
                "timestamp": "2026-08-12T09:00:00+09:00",
                "openPrice": "70900",
                "highPrice": "71100",
                "lowPrice": "70800",
                "closePrice": "71000",
                "volume": "1000",
                "currency": "KRW",
            },
        ],
        "nextBefore": "2026-08-12T09:00:00+09:00",
    }


class CandleTest(unittest.TestCase):
    def test_requires_timezone_and_valid_ohlc(self) -> None:
        with self.assertRaises(ValueError):
            Candle(
                symbol="005930",
                interval="1m",
                timestamp=datetime(2026, 8, 12, 9, 0, tzinfo=UTC).replace(tzinfo=None),
                open_price=Decimal(10),
                high_price=Decimal(9),
                low_price=Decimal(8),
                close_price=Decimal(10),
                volume=Decimal(1),
                currency="KRW",
            )


class MarketCollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SqliteMarketRepository(":memory:")

    def tearDown(self) -> None:
        self.repository.close()

    def test_collects_and_upserts_idempotently(self) -> None:
        client = FakeCandleClient(candle_payload())
        collector = MarketCollector(client=client, repository=self.repository)

        first = collector.collect(symbol="005930", interval="1m", count=2)
        second = collector.collect(symbol="005930", interval="1m", count=2)
        stored = self.repository.latest_candles("005930", "1m", limit=10)

        self.assertEqual(first.received, 2)
        self.assertEqual(first.upserted, 2)
        self.assertEqual(second.upserted, 2)
        self.assertEqual(self.repository.count("005930", "1m"), 2)
        self.assertEqual(
            [item.close_price for item in stored], [Decimal(71000), Decimal(71100)]
        )
        self.assertEqual(first.next_before, "2026-08-12T09:00:00+09:00")

    def test_collects_stock_names_into_database(self) -> None:
        collector = MarketCollector(
            client=FakeCandleClient(candle_payload()), repository=self.repository
        )

        names = collector.collect_symbol_names(("005930", "000660"))

        self.assertEqual(names["005930"], "Name 005930")
        row = self.repository._connection.execute(
            "SELECT display_name FROM market_symbols WHERE symbol = ?", ("005930",)
        ).fetchone()
        self.assertEqual(row, ("Name 005930",))

    def test_rejects_malformed_api_candle_without_writing(self) -> None:
        payload = candle_payload()
        payload["candles"][0]["highPrice"] = "not-a-number"
        collector = MarketCollector(
            client=FakeCandleClient(payload), repository=self.repository
        )

        with self.assertRaises(ValueError):
            collector.collect(symbol="005930", interval="1m", count=2)

        self.assertEqual(self.repository.count("005930", "1m"), 0)

    def test_read_repository_opens_existing_sqlite_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "market.db")
            writer = SqliteMarketRepository(path)
            writer.upsert_candles(
                [
                    Candle(
                        symbol="005930",
                        interval="1d",
                        timestamp=datetime(2026, 8, 12, tzinfo=UTC),
                        open_price=Decimal(100),
                        high_price=Decimal(100),
                        low_price=Decimal(100),
                        close_price=Decimal(100),
                        volume=Decimal(1),
                        currency="KRW",
                    )
                ]
            )
            writer.close()

            reader = SqliteMarketReadRepository(path)
            try:
                self.assertEqual(len(reader.latest_candles("005930", "1d", limit=1)), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    reader._connection.execute(
                        "INSERT INTO market_symbols VALUES ('x','x')"
                    )
            finally:
                reader.close()


class StoredMaStrategyTest(unittest.TestCase):
    def test_uses_chronological_stored_closes(self) -> None:
        repository = SqliteMarketRepository(":memory:")
        try:
            closes = [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]
            candles = [
                Candle(
                    symbol="005930",
                    interval="1d",
                    timestamp=datetime(2026, 8, 9 + index, tzinfo=UTC),
                    open_price=close,
                    high_price=close,
                    low_price=close,
                    close_price=close,
                    volume=Decimal(100),
                    currency="KRW",
                )
                for index, close in enumerate(closes)
            ]
            repository.upsert_candles(candles)

            result = StoredMaStrategy(repository).evaluate(
                symbol="005930",
                interval="1d",
                quantity=Decimal(1),
                short_window=2,
                long_window=3,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.side, Side.BUY)
            self.assertEqual(result.reference_price, Decimal(12))
        finally:
            repository.close()


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object | None]] = []
        self.batch: tuple[str, list[tuple]] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: object | None = None) -> None:
        self.executed.append((query, params))

    def executemany(self, query: str, params: list[tuple]) -> None:
        self.batch = (query, params)


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.commits = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class PostgresMarketRepositoryTest(unittest.TestCase):
    def test_read_repository_forces_read_only_without_schema_ddl(self) -> None:
        connection = FakeConnection()
        received: dict = {}

        def connect(**kwargs: object) -> FakeConnection:
            received.update(kwargs)
            return connection

        repository = PostgresMarketReadRepository(
            {
                "host": "postgres.internal",
                "port": 5432,
                "user": "reader",
                "password": "secret",
                "dbname": "toss_trader",
            },
            connect=connect,
        )
        repository.close()

        self.assertEqual(received["options"], "-c default_transaction_read_only=on")
        self.assertEqual(received["connect_timeout"], 5)
        self.assertEqual(connection.cursor_instance.executed, [])
        self.assertEqual(connection.commits, 0)

    def test_initializes_schema_and_uses_parameterized_upsert(self) -> None:
        connection = FakeConnection()
        received: dict = {}

        def connect(**kwargs: object) -> FakeConnection:
            received.update(kwargs)
            return connection

        repository = PostgresMarketRepository(
            {
                "host": "postgres.internal",
                "port": 5432,
                "user": "trader",
                "password": "secret@:/value",
                "dbname": "toss_trader",
            },
            connect=connect,
        )
        candle = Candle(
            symbol="AAPL",
            interval="1d",
            timestamp=datetime(2026, 8, 12, tzinfo=UTC),
            open_price=Decimal(200),
            high_price=Decimal(210),
            low_price=Decimal(195),
            close_price=Decimal(205),
            volume=Decimal(1000),
            currency="USD",
        )

        count = repository.upsert_candles([candle])
        repository.close()

        self.assertEqual(count, 1)
        self.assertIn("TIMESTAMPTZ", connection.cursor_instance.executed[0][0])
        self.assertIn("market_symbols", connection.cursor_instance.executed[1][0])
        assert connection.cursor_instance.batch is not None
        query, params = connection.cursor_instance.batch
        self.assertIn("VALUES (%s, %s, %s", query)
        self.assertEqual(params[0][0:2], ("AAPL", "1d"))
        self.assertEqual(connection.commits, 2)
        self.assertTrue(connection.closed)
        self.assertEqual(received["password"], "secret@:/value")


if __name__ == "__main__":
    unittest.main()
