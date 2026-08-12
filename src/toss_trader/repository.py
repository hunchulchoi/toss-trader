from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .models import Candle


class MarketRepository(Protocol):
    def upsert_candles(self, candles: Sequence[Candle]) -> int: ...

    def latest_candles(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[Candle]: ...

    def count(self, symbol: str, interval: str) -> int: ...

    def close(self) -> None: ...


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL CHECK (interval IN ('1m', '1d')),
    timestamp TEXT NOT NULL,
    open_price TEXT NOT NULL,
    high_price TEXT NOT NULL,
    low_price TEXT NOT NULL,
    close_price TEXT NOT NULL,
    volume TEXT NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, timestamp)
)
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL CHECK (interval IN ('1m', '1d')),
    timestamp TIMESTAMPTZ NOT NULL,
    open_price NUMERIC NOT NULL,
    high_price NUMERIC NOT NULL,
    low_price NUMERIC NOT NULL,
    close_price NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, timestamp)
)
"""

POSTGRES_INDEX = """
CREATE INDEX IF NOT EXISTS market_candles_latest_idx
ON market_candles (symbol, interval, timestamp DESC)
"""


class SqliteMarketRepository:
    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(SQLITE_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def upsert_candles(self, candles: Sequence[Candle]) -> int:
        if not candles:
            return 0
        rows = [_sqlite_row(candle) for candle in candles]
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO market_candles (
                    symbol, interval, timestamp, open_price, high_price,
                    low_price, close_price, volume, currency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET
                    open_price = excluded.open_price,
                    high_price = excluded.high_price,
                    low_price = excluded.low_price,
                    close_price = excluded.close_price,
                    volume = excluded.volume,
                    currency = excluded.currency
                """,
                rows,
            )
        return len(rows)

    def latest_candles(self, symbol: str, interval: str, *, limit: int) -> list[Candle]:
        _validate_limit(limit)
        rows = self._connection.execute(
            """
            SELECT symbol, interval, timestamp, open_price, high_price,
                   low_price, close_price, volume, currency
            FROM market_candles
            WHERE symbol = ? AND interval = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, interval, limit),
        ).fetchall()
        return list(reversed([_candle_from_row(row) for row in rows]))

    def count(self, symbol: str, interval: str) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM market_candles
            WHERE symbol = ? AND interval = ?
            """,
            (symbol, interval),
        ).fetchone()
        return int(row[0]) if row else 0


class PostgresMarketRepository:
    def __init__(
        self,
        connection_parameters: Mapping[str, str | int],
        *,
        connect: Callable[..., Any] | None = None,
        database_error: type[Exception] | None = None,
    ) -> None:
        required = {"host", "port", "user", "password", "dbname"}
        missing = sorted(required - connection_parameters.keys())
        if missing:
            raise ValueError(f"missing PostgreSQL parameters: {', '.join(missing)}")
        if connect is None:
            try:
                import psycopg
            except ImportError as error:
                raise RuntimeError(
                    "PostgreSQL support requires: pip install 'toss-trader[postgres]'"
                ) from error
            connect = psycopg.connect
            database_error = psycopg.Error
        self._database_error = database_error or Exception
        try:
            self._connection = connect(
                **{name: connection_parameters[name] for name in required}
            )
        except self._database_error as error:
            raise RuntimeError("PostgreSQL connection failed") from error
        with self._connection.cursor() as cursor:
            cursor.execute(POSTGRES_SCHEMA)
            cursor.execute(POSTGRES_INDEX)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def upsert_candles(self, candles: Sequence[Candle]) -> int:
        if not candles:
            return 0
        rows = [_postgres_row(candle) for candle in candles]
        with self._connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO market_candles (
                    symbol, interval, timestamp, open_price, high_price,
                    low_price, close_price, volume, currency
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    volume = EXCLUDED.volume,
                    currency = EXCLUDED.currency
                """,
                rows,
            )
        self._connection.commit()
        return len(rows)

    def latest_candles(self, symbol: str, interval: str, *, limit: int) -> list[Candle]:
        _validate_limit(limit)
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT symbol, interval, timestamp, open_price, high_price,
                       low_price, close_price, volume, currency
                FROM market_candles
                WHERE symbol = %s AND interval = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (symbol, interval, limit),
            )
            rows = cursor.fetchall()
        return list(reversed([_candle_from_row(row) for row in rows]))

    def count(self, symbol: str, interval: str) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM market_candles
                WHERE symbol = %s AND interval = %s
                """,
                (symbol, interval),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0


def open_market_repository(
    *,
    postgres_parameters: Mapping[str, str | int] | None,
    sqlite_path: str,
) -> MarketRepository:
    if postgres_parameters:
        return PostgresMarketRepository(postgres_parameters)
    return SqliteMarketRepository(sqlite_path)


def _sqlite_row(candle: Candle) -> tuple[str, ...]:
    return (
        candle.symbol,
        candle.interval,
        candle.timestamp.astimezone(UTC).isoformat(),
        str(candle.open_price),
        str(candle.high_price),
        str(candle.low_price),
        str(candle.close_price),
        str(candle.volume),
        candle.currency.upper(),
    )


def _postgres_row(candle: Candle) -> tuple[Any, ...]:
    return (
        candle.symbol,
        candle.interval,
        candle.timestamp.astimezone(UTC),
        candle.open_price,
        candle.high_price,
        candle.low_price,
        candle.close_price,
        candle.volume,
        candle.currency.upper(),
    )


def _candle_from_row(row: Sequence[Any]) -> Candle:
    timestamp = row[2]
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    return Candle(
        symbol=str(row[0]),
        interval=str(row[1]),
        timestamp=timestamp,
        open_price=Decimal(row[3]),
        high_price=Decimal(row[4]),
        low_price=Decimal(row[5]),
        close_price=Decimal(row[6]),
        volume=Decimal(row[7]),
        currency=str(row[8]),
    )


def _validate_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("limit must be positive")
