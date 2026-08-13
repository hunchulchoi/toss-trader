from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .models import PaperFill, Side, TradeSignal
from .risk import RiskContext, RiskDecision


class DuplicatePaperOrder(RuntimeError):
    pass


class PaperLedgerStore(Protocol):
    def close(self) -> None: ...

    def execute(
        self, signal: TradeSignal, *, executed_at: datetime | None = None
    ) -> PaperFill: ...

    def record_risk_decision(
        self,
        signal: TradeSignal,
        decision: RiskDecision,
        context: RiskContext,
        *,
        evaluated_at: datetime,
    ) -> str: ...

    def recent_risk_decisions(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        approved: bool | None = None,
    ) -> list[dict[str, object]]: ...

    def record_automation_run(
        self,
        *,
        run_type: str,
        status: str,
        stage: str,
        started_at: datetime,
        finished_at: datetime,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        error: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> str: ...

    def recent_automation_runs(
        self,
        *,
        limit: int = 100,
        run_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]: ...

    def daily_buy_count(self, day: date) -> int: ...

    def position_notional(
        self, symbol: str, *, mark_price: Decimal | None = None
    ) -> Decimal: ...

    def position_quantity(self, symbol: str) -> Decimal: ...

    def position_quantities(self) -> dict[str, Decimal]: ...

    def cash_balance(self, initial_cash: Decimal) -> Decimal: ...

    def seen_signal_ids(self) -> frozenset[str]: ...


class PaperLedger:
    def __init__(self, database_path: str, *, portfolio_id: str = "legacy") -> None:
        self._portfolio_id = _validate_portfolio_id(portfolio_id)
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_fills (
                fill_id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL DEFAULT 'legacy',
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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_risk_decisions (
                decision_id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL DEFAULT 'legacy',
                signal_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                quantity TEXT NOT NULL,
                reference_price TEXT NOT NULL,
                notional TEXT NOT NULL,
                signal_reason TEXT NOT NULL,
                approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                violations TEXT NOT NULL,
                position_notional TEXT NOT NULL,
                position_quantity TEXT NOT NULL,
                available_cash TEXT,
                daily_buy_count INTEGER NOT NULL,
                daily_return_rate TEXT NOT NULL,
                consecutive_api_errors INTEGER NOT NULL,
                market_is_business_day INTEGER NOT NULL,
                market_close_at TEXT,
                evaluated_at TEXT NOT NULL
            )
            """
        )
        _sqlite_add_column(
            self._connection,
            "paper_fills",
            "portfolio_id",
            "TEXT NOT NULL DEFAULT 'legacy'",
        )
        _sqlite_add_column(
            self._connection,
            "paper_risk_decisions",
            "portfolio_id",
            "TEXT NOT NULL DEFAULT 'legacy'",
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS paper_risk_decisions_time_idx
            ON paper_risk_decisions (evaluated_at DESC)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_portfolios (
                portfolio_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                mode TEXT NOT NULL,
                initial_cash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_run_logs (
                run_id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                error TEXT,
                details TEXT NOT NULL
            )
            """
        )
        if self._portfolio_id in {"rule", "hermes"}:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO paper_portfolios (
                    portfolio_id, display_name, mode, initial_cash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._portfolio_id,
                    "규칙 기반" if self._portfolio_id == "rule" else "Hermes 개입",
                    self._portfolio_id,
                    "1000000",
                    datetime.now(UTC).isoformat(),
                ),
            )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS automation_run_logs_time_idx
            ON automation_run_logs (finished_at DESC)
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
                        fill_id, portfolio_id, signal_id, symbol, side, quantity, price,
                        notional, reason, executed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.fill_id,
                        self._portfolio_id,
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

    def record_risk_decision(
        self,
        signal: TradeSignal,
        decision: RiskDecision,
        context: RiskContext,
        *,
        evaluated_at: datetime,
    ) -> str:
        decision_id = str(uuid4())
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO paper_risk_decisions (
                    decision_id, portfolio_id, signal_id, symbol, side, quantity,
                    reference_price, notional, signal_reason, approved,
                    violations, position_notional, position_quantity,
                    available_cash, daily_buy_count, daily_return_rate,
                    consecutive_api_errors, market_is_business_day,
                    market_close_at, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    _sqlite_audit_value(value)
                    for value in (
                        decision_id,
                        self._portfolio_id,
                        *_risk_decision_values(
                            decision_id, signal, decision, context, evaluated_at
                        )[1:],
                    )
                ),
            )
        return decision_id

    def recent_risk_decisions(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        approved: bool | None = None,
    ) -> list[dict[str, object]]:
        _validate_audit_query(limit, symbol)
        conditions: list[str] = ["portfolio_id = ?"]
        parameters: list[object] = [self._portfolio_id]
        if symbol is not None:
            conditions.append("symbol = ?")
            parameters.append(symbol)
        if approved is not None:
            conditions.append("approved = ?")
            parameters.append(int(approved))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._connection.execute(
            f"""
            SELECT decision_id, signal_id, symbol, side, quantity,
                   reference_price, notional, signal_reason, approved, violations,
                   position_notional, position_quantity, available_cash,
                   daily_buy_count, daily_return_rate, consecutive_api_errors,
                   market_is_business_day, market_close_at, evaluated_at
            FROM paper_risk_decisions
            {where}
            ORDER BY evaluated_at DESC, rowid DESC
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        return [_risk_decision_row(row) for row in rows]

    def record_automation_run(
        self,
        *,
        run_type: str,
        status: str,
        stage: str,
        started_at: datetime,
        finished_at: datetime,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        error: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> str:
        values = _automation_run_values(
            run_type=run_type,
            status=status,
            stage=stage,
            started_at=started_at,
            finished_at=finished_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error=error,
            details=details,
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO automation_run_logs (
                    run_id, run_type, status, stage, started_at, finished_at,
                    duration_ms, prompt_tokens, completion_tokens, total_tokens,
                    error, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(_sqlite_audit_value(value) for value in values),
            )
        return str(values[0])

    def recent_automation_runs(
        self,
        *,
        limit: int = 100,
        run_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        _validate_automation_query(limit, run_type, status)
        conditions: list[str] = []
        parameters: list[object] = []
        if run_type is not None:
            conditions.append("run_type = ?")
            parameters.append(run_type)
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._connection.execute(
            f"""
            SELECT run_id, run_type, status, stage, started_at, finished_at,
                   duration_ms, prompt_tokens, completion_tokens, total_tokens,
                   error, details
            FROM automation_run_logs
            {where}
            ORDER BY finished_at DESC, rowid DESC
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        return [_automation_run_row(row) for row in rows]

    def daily_buy_count(self, day: date) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM paper_fills
            WHERE side = 'BUY' AND substr(executed_at, 1, 10) = ?
              AND portfolio_id = ?
            """,
            (day.isoformat(), self._portfolio_id),
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
                WHERE symbol = ? AND portfolio_id = ?
                """,
                (symbol, self._portfolio_id),
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
            WHERE symbol = ? AND portfolio_id = ?
            """,
            (symbol, self._portfolio_id),
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
            "SELECT symbol, side, quantity FROM paper_fills WHERE portfolio_id = ?",
            (self._portfolio_id,),
        ).fetchall()
        positions: dict[str, Decimal] = {}
        for symbol, side, quantity in rows:
            signed = Decimal(quantity) if side == Side.BUY.value else -Decimal(quantity)
            positions[str(symbol)] = positions.get(str(symbol), Decimal(0)) + signed
        return {symbol: quantity for symbol, quantity in positions.items() if quantity}

    def cash_balance(self, initial_cash: Decimal) -> Decimal:
        rows = self._connection.execute(
            "SELECT side, notional FROM paper_fills WHERE portfolio_id = ?",
            (self._portfolio_id,),
        ).fetchall()
        cash_change = sum(
            (
                -Decimal(notional) if side == Side.BUY.value else Decimal(notional)
                for side, notional in rows
            ),
            start=Decimal(0),
        )
        return initial_cash + cash_change

    def seen_signal_ids(self) -> frozenset[str]:
        rows = self._connection.execute(
            "SELECT signal_id FROM paper_fills WHERE portfolio_id = ?",
            (self._portfolio_id,),
        ).fetchall()
        return frozenset(str(row[0]) for row in rows)


POSTGRES_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id UUID PRIMARY KEY,
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
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

POSTGRES_RISK_DECISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_risk_decisions (
    decision_id UUID PRIMARY KEY,
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    reference_price NUMERIC NOT NULL CHECK (reference_price > 0),
    notional NUMERIC NOT NULL CHECK (notional > 0),
    signal_reason TEXT NOT NULL,
    approved BOOLEAN NOT NULL,
    violations JSONB NOT NULL,
    position_notional NUMERIC NOT NULL,
    position_quantity NUMERIC NOT NULL,
    available_cash NUMERIC,
    daily_buy_count INTEGER NOT NULL,
    daily_return_rate NUMERIC NOT NULL,
    consecutive_api_errors INTEGER NOT NULL,
    market_is_business_day BOOLEAN NOT NULL,
    market_close_at TIMESTAMPTZ,
    evaluated_at TIMESTAMPTZ NOT NULL
)
"""

POSTGRES_RISK_DECISION_INDEX = """
CREATE INDEX IF NOT EXISTS paper_risk_decisions_time_idx
ON paper_risk_decisions (evaluated_at DESC)
"""

POSTGRES_AUTOMATION_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_run_logs (
    run_id UUID PRIMARY KEY,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL,
    prompt_tokens BIGINT NOT NULL,
    completion_tokens BIGINT NOT NULL,
    total_tokens BIGINT NOT NULL,
    error TEXT,
    details JSONB NOT NULL
)
"""

POSTGRES_PORTFOLIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_portfolios (
    portfolio_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    initial_cash NUMERIC NOT NULL CHECK (initial_cash > 0),
    created_at TIMESTAMPTZ NOT NULL
)
"""

POSTGRES_AUTOMATION_RUN_INDEX = """
CREATE INDEX IF NOT EXISTS automation_run_logs_time_idx
ON automation_run_logs (finished_at DESC)
"""


class PostgresPaperLedger:
    def __init__(
        self,
        connection_parameters: Mapping[str, str | int],
        *,
        connect: Callable[..., Any] | None = None,
        database_error: type[Exception] | None = None,
        portfolio_id: str = "legacy",
        initialize_schema: bool = True,
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
        if initialize_schema:
            with self._connection.cursor() as cursor:
                cursor.execute(POSTGRES_PAPER_SCHEMA)
                cursor.execute(POSTGRES_PAPER_INDEX)
                cursor.execute(POSTGRES_RISK_DECISION_SCHEMA)
                cursor.execute(POSTGRES_RISK_DECISION_INDEX)
                cursor.execute(
                    "ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS portfolio_id TEXT NOT NULL DEFAULT 'legacy'"
                )
                cursor.execute(
                    "ALTER TABLE paper_risk_decisions ADD COLUMN IF NOT EXISTS portfolio_id TEXT NOT NULL DEFAULT 'legacy'"
                )
                cursor.execute(POSTGRES_AUTOMATION_RUN_SCHEMA)
                cursor.execute(POSTGRES_AUTOMATION_RUN_INDEX)
                cursor.execute(POSTGRES_PORTFOLIO_SCHEMA)
                if self._portfolio_id in {"rule", "hermes"}:
                    cursor.execute(
                        """
                        INSERT INTO paper_portfolios (
                            portfolio_id, display_name, mode, initial_cash, created_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (portfolio_id) DO NOTHING
                        """,
                        (
                            self._portfolio_id,
                            "규칙 기반"
                            if self._portfolio_id == "rule"
                            else "Hermes 개입",
                            self._portfolio_id,
                            Decimal(1000000),
                            datetime.now(UTC),
                        ),
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
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO paper_fills (
                        fill_id, portfolio_id, signal_id, symbol, side, quantity, price,
                        notional, reason, executed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fill.fill_id,
                        self._portfolio_id,
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

    def record_risk_decision(
        self,
        signal: TradeSignal,
        decision: RiskDecision,
        context: RiskContext,
        *,
        evaluated_at: datetime,
    ) -> str:
        decision_id = str(uuid4())
        values = _risk_decision_values(
            decision_id, signal, decision, context, evaluated_at
        )
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO paper_risk_decisions (
                        decision_id, portfolio_id, signal_id, symbol, side, quantity,
                        reference_price, notional, signal_reason, approved,
                        violations, position_notional, position_quantity,
                        available_cash, daily_buy_count, daily_return_rate,
                        consecutive_api_errors, market_is_business_day,
                        market_close_at, evaluated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (values[0], self._portfolio_id, *values[1:]),
                )
            self._connection.commit()
        except self._database_error:
            self._connection.rollback()
            raise
        return decision_id

    def recent_risk_decisions(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        approved: bool | None = None,
    ) -> list[dict[str, object]]:
        _validate_audit_query(limit, symbol)
        conditions: list[str] = ["portfolio_id = %s"]
        parameters: list[object] = [self._portfolio_id]
        if symbol is not None:
            conditions.append("symbol = %s")
            parameters.append(symbol)
        if approved is not None:
            conditions.append("approved = %s")
            parameters.append(approved)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT decision_id, signal_id, symbol, side, quantity,
                       reference_price, notional, signal_reason, approved, violations,
                       position_notional, position_quantity, available_cash,
                       daily_buy_count, daily_return_rate, consecutive_api_errors,
                       market_is_business_day, market_close_at, evaluated_at
                FROM paper_risk_decisions
                {where}
                ORDER BY evaluated_at DESC, decision_id DESC
                LIMIT %s
                """,
                (*parameters, limit),
            )
            rows = cursor.fetchall()
        return [_risk_decision_row(row) for row in rows]

    def record_automation_run(
        self,
        *,
        run_type: str,
        status: str,
        stage: str,
        started_at: datetime,
        finished_at: datetime,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        error: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> str:
        values = _automation_run_values(
            run_type=run_type,
            status=status,
            stage=stage,
            started_at=started_at,
            finished_at=finished_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error=error,
            details=details,
        )
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO automation_run_logs (
                        run_id, run_type, status, stage, started_at, finished_at,
                        duration_ms, prompt_tokens, completion_tokens, total_tokens,
                        error, details
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                    )
                    """,
                    values,
                )
            self._connection.commit()
        except self._database_error:
            self._connection.rollback()
            raise
        return str(values[0])

    def recent_automation_runs(
        self,
        *,
        limit: int = 100,
        run_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        _validate_automation_query(limit, run_type, status)
        conditions: list[str] = []
        parameters: list[object] = []
        if run_type is not None:
            conditions.append("run_type = %s")
            parameters.append(run_type)
        if status is not None:
            conditions.append("status = %s")
            parameters.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT run_id, run_type, status, stage, started_at, finished_at,
                       duration_ms, prompt_tokens, completion_tokens, total_tokens,
                       error, details
                FROM automation_run_logs
                {where}
                ORDER BY finished_at DESC, run_id DESC
                LIMIT %s
                """,
                (*parameters, limit),
            )
            rows = cursor.fetchall()
        return [_automation_run_row(row) for row in rows]

    def daily_buy_count(self, day: date) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM paper_fills
                WHERE side = 'BUY'
                  AND (executed_at AT TIME ZONE 'UTC')::date = %s
                  AND portfolio_id = %s
                """,
                (day, self._portfolio_id),
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
                FROM paper_fills WHERE symbol = %s AND portfolio_id = %s
                """,
                (symbol, self._portfolio_id),
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
                FROM paper_fills WHERE symbol = %s AND portfolio_id = %s
                """,
                (symbol, self._portfolio_id),
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
                WHERE portfolio_id = %s
                GROUP BY symbol
                HAVING COALESCE(SUM(
                    CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END
                ), 0) <> 0
                """,
                (self._portfolio_id,),
            )
            rows = cursor.fetchall()
        return {str(symbol): Decimal(quantity) for symbol, quantity in rows}

    def cash_balance(self, initial_cash: Decimal) -> Decimal:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN side = 'BUY' THEN -notional ELSE notional END
                ), 0)
                FROM paper_fills
                WHERE portfolio_id = %s
                """,
                (self._portfolio_id,),
            )
            row = cursor.fetchone()
        return initial_cash + (Decimal(row[0]) if row else Decimal(0))

    def seen_signal_ids(self) -> frozenset[str]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT signal_id FROM paper_fills WHERE portfolio_id = %s",
                (self._portfolio_id,),
            )
            rows = cursor.fetchall()
        return frozenset(str(row[0]) for row in rows)


def open_paper_ledger(
    *,
    postgres_parameters: Mapping[str, str | int] | None,
    sqlite_path: str,
    portfolio_id: str = "legacy",
    initialize_schema: bool = True,
) -> PaperLedgerStore:
    if postgres_parameters:
        return PostgresPaperLedger(
            postgres_parameters,
            portfolio_id=portfolio_id,
            initialize_schema=initialize_schema,
        )
    return PaperLedger(sqlite_path, portfolio_id=portfolio_id)


def _validate_portfolio_id(portfolio_id: str) -> str:
    if portfolio_id not in {"legacy", "rule", "hermes", "comparison"}:
        raise ValueError("invalid paper portfolio id")
    return portfolio_id


def _sqlite_add_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _risk_decision_values(
    decision_id: str,
    signal: TradeSignal,
    decision: RiskDecision,
    context: RiskContext,
    evaluated_at: datetime,
) -> tuple[object, ...]:
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("risk decision timestamp must include timezone")
    return (
        decision_id,
        signal.signal_id,
        signal.symbol,
        signal.side.value,
        signal.quantity,
        signal.reference_price,
        signal.notional,
        signal.reason,
        decision.approved,
        json.dumps(decision.violations, ensure_ascii=False),
        context.position_notional,
        context.position_quantity,
        context.available_cash,
        context.daily_buy_count,
        context.daily_return_rate,
        context.consecutive_api_errors,
        context.market_is_business_day,
        context.market_close_at,
        evaluated_at,
    )


def _automation_run_values(
    *,
    run_type: str,
    status: str,
    stage: str,
    started_at: datetime,
    finished_at: datetime,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    error: str | None,
    details: Mapping[str, object] | None,
) -> tuple[object, ...]:
    if run_type not in {"daily", "market_scan", "hermes_trade", "n8n_flow"}:
        raise ValueError("unknown automation run type")
    if status not in {"succeeded", "failed", "skipped"}:
        raise ValueError("unknown automation run status")
    if not stage.strip():
        raise ValueError("automation run stage must not be empty")
    if any(
        value.tzinfo is None or value.utcoffset() is None
        for value in (started_at, finished_at)
    ):
        raise ValueError("automation run timestamps must include timezone")
    if finished_at < started_at:
        raise ValueError("automation run cannot finish before it starts")
    token_values = (prompt_tokens, completion_tokens, total_tokens)
    if any(isinstance(value, bool) or value < 0 for value in token_values):
        raise ValueError("automation token counts must be non-negative integers")
    if prompt_tokens + completion_tokens > total_tokens:
        raise ValueError("automation total tokens are inconsistent")
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    return (
        str(uuid4()),
        run_type,
        status,
        stage,
        started_at,
        finished_at,
        duration_ms,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        error,
        json.dumps(details or {}, ensure_ascii=False, default=str),
    )


def _validate_audit_query(limit: int, symbol: str | None) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("risk decision limit must be between 1 and 1000")
    if symbol is not None and not symbol.strip():
        raise ValueError("risk decision symbol must not be empty")


def _validate_automation_query(
    limit: int, run_type: str | None, status: str | None
) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("automation run limit must be between 1 and 1000")
    if run_type is not None and run_type not in {
        "daily",
        "market_scan",
        "hermes_trade",
        "n8n_flow",
    }:
        raise ValueError("unknown automation run type")
    if status is not None and status not in {"succeeded", "failed", "skipped"}:
        raise ValueError("unknown automation run status")


def _sqlite_audit_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def _risk_decision_row(row: Sequence[object]) -> dict[str, object]:
    values = tuple(row)
    raw_violations = values[9]
    violations = (
        json.loads(raw_violations)
        if isinstance(raw_violations, str)
        else list(raw_violations)
    )
    return {
        "decisionId": str(values[0]),
        "signalId": str(values[1]),
        "symbol": str(values[2]),
        "side": str(values[3]),
        "quantity": str(values[4]),
        "referencePrice": str(values[5]),
        "notional": str(values[6]),
        "signalReason": str(values[7]),
        "approved": bool(values[8]),
        "violations": violations,
        "positionNotional": str(values[10]),
        "positionQuantity": str(values[11]),
        "availableCash": str(values[12]) if values[12] is not None else None,
        "dailyBuyCount": int(values[13]),
        "dailyReturnRate": str(values[14]),
        "consecutiveApiErrors": int(values[15]),
        "marketIsBusinessDay": bool(values[16]),
        "marketCloseAt": (
            values[17].isoformat()
            if isinstance(values[17], datetime)
            else str(values[17])
            if values[17] is not None
            else None
        ),
        "evaluatedAt": (
            values[18].isoformat()
            if isinstance(values[18], datetime)
            else str(values[18])
        ),
    }


def _automation_run_row(row: Sequence[object]) -> dict[str, object]:
    values = tuple(row)
    raw_details = values[11]
    details = (
        json.loads(raw_details) if isinstance(raw_details, str) else dict(raw_details)
    )
    return {
        "runId": str(values[0]),
        "runType": str(values[1]),
        "status": str(values[2]),
        "stage": str(values[3]),
        "startedAt": _serialized_datetime(values[4]),
        "finishedAt": _serialized_datetime(values[5]),
        "durationMs": int(values[6]),
        "promptTokens": int(values[7]),
        "completionTokens": int(values[8]),
        "totalTokens": int(values[9]),
        "error": str(values[10]) if values[10] is not None else None,
        "details": details,
    }


def _serialized_datetime(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)
