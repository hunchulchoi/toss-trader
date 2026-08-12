from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .models import PaperFill, Side, TradeSignal


class DuplicatePaperOrder(RuntimeError):
    pass


class PaperLedgerStore(Protocol):
    def close(self) -> None: ...

    def execute(
        self, signal: TradeSignal, *, executed_at: datetime | None = None
    ) -> PaperFill: ...

    def daily_buy_count(self, day: date) -> int: ...

    def position_notional(
        self, symbol: str, *, mark_price: Decimal | None = None
    ) -> Decimal: ...

    def position_quantity(self, symbol: str) -> Decimal: ...

    def position_quantities(self) -> dict[str, Decimal]: ...

    def seen_signal_ids(self) -> frozenset[str]: ...


class PaperLedger:
    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_fills (
                fill_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                quantity TEXT NOT NULL,
                price TEXT NOT NULL,
                notional TEXT NOT NULL,
                reason TEXT NOT NULL,
                executed_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def execute(
        self, signal: TradeSignal, *, executed_at: datetime | None = None
    ) -> PaperFill:
        when = executed_at or datetime.now(UTC)
        fill = PaperFill(
            fill_id=str(uuid4()),
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.reference_price,
            notional=signal.notional,
            reason=signal.reason,
            executed_at=when,
        )
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO paper_fills (
                        fill_id, signal_id, symbol, side, quantity, price,
                        notional, reason, executed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.fill_id,
                        fill.signal_id,
                        fill.symbol,
                        fill.side.value,
                        str(fill.quantity),
                        str(fill.price),
                        str(fill.notional),
                        fill.reason,
                        fill.executed_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicatePaperOrder(
                f"paper fill already exists for signal_id={signal.signal_id}"
            ) from error
        return fill

    def daily_buy_count(self, day: date) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM paper_fills
            WHERE side = 'BUY' AND substr(executed_at, 1, 10) = ?
            """,
            (day.isoformat(),),
        ).fetchone()
        return int(row[0]) if row else 0

    def position_notional(
        self, symbol: str, *, mark_price: Decimal | None = None
    ) -> Decimal:
        if mark_price is None:
            row = self._connection.execute(
                """
                SELECT side, notional
                FROM paper_fills
                WHERE symbol = ?
                """,
                (symbol,),
            ).fetchall()
            return sum(
                (
                    Decimal(notional) if side == Side.BUY.value else -Decimal(notional)
                    for side, notional in row
                ),
                start=Decimal(0),
            )
        return self.position_quantity(symbol) * mark_price

    def position_quantity(self, symbol: str) -> Decimal:
        rows = self._connection.execute(
            """
            SELECT side, quantity
            FROM paper_fills
            WHERE symbol = ?
            """,
            (symbol,),
        ).fetchall()
        return sum(
            (
                Decimal(quantity) if side == Side.BUY.value else -Decimal(quantity)
                for side, quantity in rows
            ),
            start=Decimal(0),
        )

    def position_quantities(self) -> dict[str, Decimal]:
        rows = self._connection.execute(
            "SELECT symbol, side, quantity FROM paper_fills"
        ).fetchall()
        positions: dict[str, Decimal] = {}
        for symbol, side, quantity in rows:
            signed = Decimal(quantity) if side == Side.BUY.value else -Decimal(quantity)
            positions[str(symbol)] = positions.get(str(symbol), Decimal(0)) + signed
        return {symbol: quantity for symbol, quantity in positions.items() if quantity}

    def seen_signal_ids(self) -> frozenset[str]:
        rows = self._connection.execute("SELECT signal_id FROM paper_fills").fetchall()
        return frozenset(str(row[0]) for row in rows)


POSTGRES_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id UUID PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    price NUMERIC NOT NULL CHECK (price > 0),
    notional NUMERIC NOT NULL CHECK (notional > 0),
    reason TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL
)
"""

POSTGRES_PAPER_INDEX = """
CREATE INDEX IF NOT EXISTS paper_fills_symbol_time_idx
ON paper_fills (symbol, executed_at DESC)
"""


class PostgresPaperLedger:
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
            cursor.execute(POSTGRES_PAPER_SCHEMA)
            cursor.execute(POSTGRES_PAPER_INDEX)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def execute(
        self, signal: TradeSignal, *, executed_at: datetime | None = None
    ) -> PaperFill:
        when = executed_at or datetime.now(UTC)
        fill = PaperFill(
            fill_id=str(uuid4()),
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.reference_price,
            notional=signal.notional,
            reason=signal.reason,
            executed_at=when,
        )
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO paper_fills (
                        fill_id, signal_id, symbol, side, quantity, price,
                        notional, reason, executed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fill.fill_id,
                        fill.signal_id,
                        fill.symbol,
                        fill.side.value,
                        fill.quantity,
                        fill.price,
                        fill.notional,
                        fill.reason,
                        fill.executed_at,
                    ),
                )
            self._connection.commit()
        except self._database_error as error:
            self._connection.rollback()
            if getattr(error, "sqlstate", None) == "23505":
                raise DuplicatePaperOrder(
                    f"paper fill already exists for signal_id={signal.signal_id}"
                ) from error
            raise
        return fill

    def daily_buy_count(self, day: date) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM paper_fills
                WHERE side = 'BUY'
                  AND (executed_at AT TIME ZONE 'UTC')::date = %s
                """,
                (day,),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def position_notional(
        self, symbol: str, *, mark_price: Decimal | None = None
    ) -> Decimal:
        if mark_price is not None:
            return self.position_quantity(symbol) * mark_price
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN side = 'BUY' THEN notional ELSE -notional END
                ), 0)
                FROM paper_fills WHERE symbol = %s
                """,
                (symbol,),
            )
            row = cursor.fetchone()
        return Decimal(row[0]) if row else Decimal(0)

    def position_quantity(self, symbol: str) -> Decimal:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END
                ), 0)
                FROM paper_fills WHERE symbol = %s
                """,
                (symbol,),
            )
            row = cursor.fetchone()
        return Decimal(row[0]) if row else Decimal(0)

    def position_quantities(self) -> dict[str, Decimal]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT symbol, COALESCE(SUM(
                    CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END
                ), 0)
                FROM paper_fills
                GROUP BY symbol
                HAVING COALESCE(SUM(
                    CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END
                ), 0) <> 0
                """
            )
            rows = cursor.fetchall()
        return {str(symbol): Decimal(quantity) for symbol, quantity in rows}

    def seen_signal_ids(self) -> frozenset[str]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT signal_id FROM paper_fills")
            rows = cursor.fetchall()
        return frozenset(str(row[0]) for row in rows)


def open_paper_ledger(
    *,
    postgres_parameters: Mapping[str, str | int] | None,
    sqlite_path: str,
) -> PaperLedgerStore:
    if postgres_parameters:
        return PostgresPaperLedger(postgres_parameters)
    return PaperLedger(sqlite_path)
