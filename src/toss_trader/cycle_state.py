from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

RUN_STATUSES = frozenset({"running", "succeeded", "partial_failure", "failed"})


@dataclass(frozen=True, slots=True)
class PaperCycleRun:
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    interval: str
    symbol_count: int
    signal_count: int
    fill_count: int
    failed_count: int
    consecutive_api_errors: int
    daily_return_rate: Decimal
    error_message: str | None


class CycleStateStore(Protocol):
    def close(self) -> None: ...

    def start_run(
        self, *, started_at: datetime, interval: str, symbol_count: int
    ) -> str: ...

    def finish_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        status: str,
        signal_count: int,
        fill_count: int,
        failed_count: int,
        consecutive_api_errors: int,
        daily_return_rate: Decimal,
        error_message: str | None,
    ) -> None: ...

    def latest_consecutive_api_errors(self) -> int: ...

    def latest_run(self) -> PaperCycleRun | None: ...


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_cycle_runs (
    run_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'succeeded', 'partial_failure', 'failed')
    ),
    interval TEXT NOT NULL CHECK (interval IN ('1m', '1d')),
    symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
    signal_count INTEGER NOT NULL DEFAULT 0 CHECK (signal_count >= 0),
    fill_count INTEGER NOT NULL DEFAULT 0 CHECK (fill_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    consecutive_api_errors INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_api_errors >= 0),
    daily_return_rate TEXT NOT NULL DEFAULT '0',
    error_message TEXT
)
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_cycle_runs (
    run_id UUID PRIMARY KEY,
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'succeeded', 'partial_failure', 'failed')
    ),
    interval TEXT NOT NULL CHECK (interval IN ('1m', '1d')),
    symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
    signal_count INTEGER NOT NULL DEFAULT 0 CHECK (signal_count >= 0),
    fill_count INTEGER NOT NULL DEFAULT 0 CHECK (fill_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    consecutive_api_errors INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_api_errors >= 0),
    daily_return_rate NUMERIC NOT NULL DEFAULT 0,
    error_message TEXT
)
"""

POSTGRES_INDEX = """
CREATE INDEX IF NOT EXISTS paper_cycle_runs_started_idx
ON paper_cycle_runs (started_at DESC)
"""


class SqliteCycleStateStore:
    def __init__(self, database_path: str, *, portfolio_id: str = "legacy") -> None:
        self._portfolio_id = _validate_portfolio_id(portfolio_id)
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(SQLITE_SCHEMA)
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(paper_cycle_runs)")
        }
        if "portfolio_id" not in columns:
            self._connection.execute(
                "ALTER TABLE paper_cycle_runs ADD COLUMN portfolio_id TEXT NOT NULL DEFAULT 'legacy'"
            )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def start_run(
        self, *, started_at: datetime, interval: str, symbol_count: int
    ) -> str:
        _validate_start(started_at, interval, symbol_count)
        run_id = str(uuid4())
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO paper_cycle_runs (
                    run_id, portfolio_id, started_at, status, interval, symbol_count
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, self._portfolio_id, started_at.isoformat(), interval, symbol_count),
            )
        return run_id

    def finish_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        status: str,
        signal_count: int,
        fill_count: int,
        failed_count: int,
        consecutive_api_errors: int,
        daily_return_rate: Decimal,
        error_message: str | None,
    ) -> None:
        _validate_finish(
            finished_at,
            status,
            signal_count,
            fill_count,
            failed_count,
            consecutive_api_errors,
        )
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE paper_cycle_runs SET
                    finished_at = ?, status = ?, signal_count = ?, fill_count = ?,
                    failed_count = ?, consecutive_api_errors = ?,
                    daily_return_rate = ?, error_message = ?
                WHERE run_id = ?
                """,
                (
                    finished_at.isoformat(),
                    status,
                    signal_count,
                    fill_count,
                    failed_count,
                    consecutive_api_errors,
                    str(daily_return_rate),
                    error_message,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"unknown paper cycle run: {run_id}")

    def latest_consecutive_api_errors(self) -> int:
        row = self._connection.execute(
            """
            SELECT consecutive_api_errors FROM paper_cycle_runs
            WHERE status <> 'running'
              AND portfolio_id = ?
            ORDER BY started_at DESC, run_id DESC
            LIMIT 1
            """,
            (self._portfolio_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def latest_run(self) -> PaperCycleRun | None:
        row = self._connection.execute(
            """
            SELECT run_id, started_at, finished_at, status, interval,
                   symbol_count, signal_count, fill_count, failed_count,
                   consecutive_api_errors, daily_return_rate, error_message
            FROM paper_cycle_runs
            WHERE portfolio_id = ?
            ORDER BY started_at DESC, run_id DESC
            LIMIT 1
            """,
            (self._portfolio_id,),
        ).fetchone()
        return _run_from_row(row) if row else None


class PostgresCycleStateStore:
    def __init__(
        self,
        connection_parameters: Mapping[str, str | int],
        *,
        connect: Callable[..., Any] | None = None,
        database_error: type[Exception] | None = None,
        portfolio_id: str = "legacy",
    ) -> None:
        self._portfolio_id = _validate_portfolio_id(portfolio_id)
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
            cursor.execute(
                "ALTER TABLE paper_cycle_runs ADD COLUMN IF NOT EXISTS portfolio_id TEXT NOT NULL DEFAULT 'legacy'"
            )
            cursor.execute(POSTGRES_INDEX)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def start_run(
        self, *, started_at: datetime, interval: str, symbol_count: int
    ) -> str:
        _validate_start(started_at, interval, symbol_count)
        run_id = str(uuid4())
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO paper_cycle_runs (
                    run_id, portfolio_id, started_at, status, interval, symbol_count
                ) VALUES (%s, %s, %s, 'running', %s, %s)
                """,
                (run_id, self._portfolio_id, started_at, interval, symbol_count),
            )
        self._connection.commit()
        return run_id

    def finish_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        status: str,
        signal_count: int,
        fill_count: int,
        failed_count: int,
        consecutive_api_errors: int,
        daily_return_rate: Decimal,
        error_message: str | None,
    ) -> None:
        _validate_finish(
            finished_at,
            status,
            signal_count,
            fill_count,
            failed_count,
            consecutive_api_errors,
        )
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE paper_cycle_runs SET
                    finished_at = %s, status = %s, signal_count = %s,
                    fill_count = %s, failed_count = %s,
                    consecutive_api_errors = %s, daily_return_rate = %s,
                    error_message = %s
                WHERE run_id = %s
                """,
                (
                    finished_at,
                    status,
                    signal_count,
                    fill_count,
                    failed_count,
                    consecutive_api_errors,
                    daily_return_rate,
                    error_message,
                    run_id,
                ),
            )
        self._connection.commit()

    def latest_consecutive_api_errors(self) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT consecutive_api_errors FROM paper_cycle_runs
                WHERE status <> 'running'
                  AND portfolio_id = %s
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """
                , (self._portfolio_id,)
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def latest_run(self) -> PaperCycleRun | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, started_at, finished_at, status, interval,
                       symbol_count, signal_count, fill_count, failed_count,
                       consecutive_api_errors, daily_return_rate, error_message
                FROM paper_cycle_runs
                WHERE portfolio_id = %s
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """
                , (self._portfolio_id,)
            )
            row = cursor.fetchone()
        return _run_from_row(row) if row else None


def open_cycle_state_store(
    *,
    postgres_parameters: Mapping[str, str | int] | None,
    sqlite_path: str,
    portfolio_id: str = "legacy",
) -> CycleStateStore:
    if postgres_parameters:
        return PostgresCycleStateStore(postgres_parameters, portfolio_id=portfolio_id)
    return SqliteCycleStateStore(sqlite_path, portfolio_id=portfolio_id)


def _validate_portfolio_id(portfolio_id: str) -> str:
    if portfolio_id not in {"legacy", "rule", "hermes", "comparison"}:
        raise ValueError("invalid paper portfolio id")
    return portfolio_id


def _validate_start(started_at: datetime, interval: str, symbol_count: int) -> None:
    _require_aware(started_at, "started_at")
    if interval not in {"1m", "1d"}:
        raise ValueError("interval must be 1m or 1d")
    if symbol_count < 0:
        raise ValueError("symbol_count must not be negative")


def _validate_finish(
    finished_at: datetime,
    status: str,
    signal_count: int,
    fill_count: int,
    failed_count: int,
    consecutive_api_errors: int,
) -> None:
    _require_aware(finished_at, "finished_at")
    if status not in RUN_STATUSES - {"running"}:
        raise ValueError("invalid completed paper cycle status")
    if min(signal_count, fill_count, failed_count, consecutive_api_errors) < 0:
        raise ValueError("paper cycle counts must not be negative")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")


def _run_from_row(row: Sequence[Any]) -> PaperCycleRun:
    started_at = _datetime(row[1])
    finished_at = _datetime(row[2]) if row[2] is not None else None
    return PaperCycleRun(
        run_id=str(row[0]),
        started_at=started_at,
        finished_at=finished_at,
        status=str(row[3]),
        interval=str(row[4]),
        symbol_count=int(row[5]),
        signal_count=int(row[6]),
        fill_count=int(row[7]),
        failed_count=int(row[8]),
        consecutive_api_errors=int(row[9]),
        daily_return_rate=Decimal(row[10]),
        error_message=str(row[11]) if row[11] is not None else None,
    )


def _datetime(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
