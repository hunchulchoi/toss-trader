from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .models import PaperFill, Side, TradeSignal, V2PositionPlan
from .risk import RiskContext, RiskDecision


class DuplicatePaperOrder(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TradeCosts:
    commission: Decimal
    tax: Decimal

    @property
    def total(self) -> Decimal:
        return self.commission + self.tax


@dataclass(frozen=True, slots=True)
class PositionAccounting:
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal
    commission: Decimal
    tax: Decimal

    @property
    def total_costs(self) -> Decimal:
        return self.commission + self.tax


TOSS_KRX_COMMISSION_RATE = Decimal("0.00015")
TOSS_US_COMMISSION_RATE = Decimal("0.001")
KOREAN_STOCK_SELL_TAX_RATE_2026 = Decimal("0.002")


def toss_trade_costs(signal: TradeSignal) -> TradeCosts:
    """Return standard Toss Open API costs for one simulated order.

    Six-digit numeric symbols are treated as KRX-listed common stocks. Other
    symbols use the US stock schedule. Paper fills currently have no venue or
    security-type field, so NXT and tax-exempt Korean ETFs are out of scope.
    """
    if len(signal.symbol) == 6 and signal.symbol.isdigit():
        return TradeCosts(
            commission=_round_down(
                signal.notional * TOSS_KRX_COMMISSION_RATE, Decimal(1)
            ),
            tax=(
                _round_down(
                    signal.notional * KOREAN_STOCK_SELL_TAX_RATE_2026,
                    Decimal(1),
                )
                if signal.side is Side.SELL
                else Decimal(0)
            ),
        )
    commission = (
        Decimal(0)
        if signal.notional <= Decimal(10)
        else _round_down(
            signal.notional * TOSS_US_COMMISSION_RATE, Decimal("0.01")
        )
    )
    return TradeCosts(commission=commission, tax=Decimal(0))


def _round_down(value: Decimal, unit: Decimal) -> Decimal:
    return value.quantize(unit, rounding=ROUND_DOWN)


class PaperLedgerStore(Protocol):
    def close(self) -> None: ...

    def execute(
        self, signal: TradeSignal, *, executed_at: datetime | None = None
    ) -> PaperFill: ...

    def estimate_costs(self, signal: TradeSignal) -> TradeCosts: ...

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

    def position_accounting(self, symbol: str) -> PositionAccounting: ...

    def position_accountings(self) -> dict[str, PositionAccounting]: ...

    def daily_equity_baseline(self, captured_at: datetime) -> Decimal | None: ...

    def record_daily_equity_baseline(
        self, *, captured_at: datetime, equity: Decimal
    ) -> None: ...

    def record_portfolio_snapshot(
        self,
        *,
        captured_at: datetime,
        equity: Decimal,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        total_costs: Decimal,
    ) -> str: ...

    def cash_balance(self, initial_cash: Decimal) -> Decimal: ...

    def seen_signal_ids(self) -> frozenset[str]: ...

    def upsert_v2_position_plan(self, plan: V2PositionPlan) -> None: ...

    def v2_position_plan(self, symbol: str) -> V2PositionPlan | None: ...

    def v2_position_plans(self) -> dict[str, V2PositionPlan]: ...

    def mark_v2_exit_pending(
        self, symbol: str, *, reason: str, triggered_at: datetime
    ) -> None: ...

    def delete_v2_position_plan(self, symbol: str) -> None: ...


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
                commission TEXT NOT NULL DEFAULT '0',
                tax TEXT NOT NULL DEFAULT '0',
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
            self._connection, "paper_fills", "commission", "TEXT NOT NULL DEFAULT '0'"
        )
        _sqlite_add_column(
            self._connection, "paper_fills", "tax", "TEXT NOT NULL DEFAULT '0'"
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
            CREATE TABLE IF NOT EXISTS paper_portfolio_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL DEFAULT 'legacy',
                captured_at TEXT NOT NULL,
                equity TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                unrealized_pnl TEXT NOT NULL,
                total_costs TEXT NOT NULL,
                UNIQUE (portfolio_id, captured_at)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_portfolio_daily_baselines (
                portfolio_id TEXT NOT NULL DEFAULT 'legacy',
                trading_day TEXT NOT NULL,
                equity TEXT NOT NULL,
                PRIMARY KEY (portfolio_id, trading_day)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_v2_position_plans (
                portfolio_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                setup_session TEXT NOT NULL,
                setups TEXT NOT NULL,
                quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                stop_price TEXT NOT NULL,
                planned_heat TEXT NOT NULL,
                ma50 TEXT NOT NULL,
                signal_close TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                exit_pending_reason TEXT,
                exit_triggered_at TEXT,
                PRIMARY KEY (portfolio_id, symbol)
            )
            """
        )
        _sqlite_add_column(
            self._connection,
            "paper_v2_position_plans",
            "cluster_id",
            "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        )
        _sqlite_add_column(
            self._connection,
            "paper_v2_position_plans",
            "signal_close",
            "TEXT NOT NULL DEFAULT '1'",
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
        costs = self.estimate_costs(signal)
        fill = PaperFill(
            fill_id=str(uuid4()),
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.reference_price,
            notional=signal.notional,
            commission=costs.commission,
            tax=costs.tax,
            reason=signal.reason,
            executed_at=when,
        )
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO paper_fills (
                        fill_id, portfolio_id, signal_id, symbol, side, quantity, price,
                        notional, commission, tax, reason, executed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        str(fill.commission),
                        str(fill.tax),
                        fill.reason,
                        fill.executed_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicatePaperOrder(
                f"paper fill already exists for signal_id={signal.signal_id}"
            ) from error
        return fill

    def estimate_costs(self, signal: TradeSignal) -> TradeCosts:
        return toss_trade_costs(signal)

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
            return self.position_accounting(symbol).cost_basis
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

    def position_accounting(self, symbol: str) -> PositionAccounting:
        return self.position_accountings().get(
            symbol,
            PositionAccounting(
                symbol=symbol,
                quantity=Decimal(0),
                cost_basis=Decimal(0),
                realized_pnl=Decimal(0),
                commission=Decimal(0),
                tax=Decimal(0),
            ),
        )

    def position_accountings(self) -> dict[str, PositionAccounting]:
        rows = self._connection.execute(
            """
            SELECT symbol, side, quantity, notional, commission, tax
            FROM paper_fills
            WHERE portfolio_id = ?
            ORDER BY executed_at, rowid
            """,
            (self._portfolio_id,),
        ).fetchall()
        return _position_accountings(rows)

    def daily_equity_baseline(self, captured_at: datetime) -> Decimal | None:
        row = self._connection.execute(
            """
            SELECT equity FROM paper_portfolio_daily_baselines
            WHERE portfolio_id = ? AND trading_day = ?
            """,
            (self._portfolio_id, _utc_trading_day(captured_at).isoformat()),
        ).fetchone()
        return Decimal(row[0]) if row else None

    def record_daily_equity_baseline(
        self, *, captured_at: datetime, equity: Decimal
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO paper_portfolio_daily_baselines (
                    portfolio_id, trading_day, equity
                ) VALUES (?, ?, ?)
                """,
                (
                    self._portfolio_id,
                    _utc_trading_day(captured_at).isoformat(),
                    str(equity),
                ),
            )

    def record_portfolio_snapshot(
        self,
        *,
        captured_at: datetime,
        equity: Decimal,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        total_costs: Decimal,
    ) -> str:
        snapshot_id = str(uuid4())
        captured = _aware_utc(captured_at)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO paper_portfolio_snapshots (
                    snapshot_id, portfolio_id, captured_at, equity,
                    realized_pnl, unrealized_pnl, total_costs
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (portfolio_id, captured_at) DO UPDATE SET
                    equity = excluded.equity,
                    realized_pnl = excluded.realized_pnl,
                    unrealized_pnl = excluded.unrealized_pnl,
                    total_costs = excluded.total_costs
                """,
                (
                    snapshot_id,
                    self._portfolio_id,
                    captured.isoformat(),
                    str(equity),
                    str(realized_pnl),
                    str(unrealized_pnl),
                    str(total_costs),
                ),
            )
        return snapshot_id

    def cash_balance(self, initial_cash: Decimal) -> Decimal:
        rows = self._connection.execute(
            "SELECT side, notional, commission, tax FROM paper_fills WHERE portfolio_id = ?",
            (self._portfolio_id,),
        ).fetchall()
        cash_change = sum(
            (
                -Decimal(notional) - Decimal(commission) - Decimal(tax)
                if side == Side.BUY.value
                else Decimal(notional) - Decimal(commission) - Decimal(tax)
                for side, notional, commission, tax in rows
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

    def upsert_v2_position_plan(self, plan: V2PositionPlan) -> None:
        with self._connection:
            self._connection.execute(
                """INSERT INTO paper_v2_position_plans (
                    portfolio_id, symbol, cluster_id, setup_session, setups, quantity,
                    entry_price, stop_price, planned_heat, ma50, signal_close,
                    opened_at, exit_pending_reason, exit_triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (portfolio_id, symbol) DO UPDATE SET
                    cluster_id=excluded.cluster_id,
                    setup_session=excluded.setup_session,
                    setups=excluded.setups,
                    quantity=excluded.quantity,
                    entry_price=excluded.entry_price,
                    stop_price=excluded.stop_price,
                    planned_heat=excluded.planned_heat,
                    ma50=excluded.ma50,
                    signal_close=excluded.signal_close,
                    opened_at=excluded.opened_at,
                    exit_pending_reason=excluded.exit_pending_reason,
                    exit_triggered_at=excluded.exit_triggered_at""",
                _v2_plan_values(self._portfolio_id, plan, serialize=True),
            )

    def v2_position_plan(self, symbol: str) -> V2PositionPlan | None:
        row = self._connection.execute(
            """SELECT symbol, cluster_id, setup_session, setups, quantity, entry_price,
                      stop_price, planned_heat, ma50, signal_close, opened_at,
                      exit_pending_reason, exit_triggered_at
               FROM paper_v2_position_plans
               WHERE portfolio_id=? AND symbol=?""",
            (self._portfolio_id, symbol),
        ).fetchone()
        return _v2_plan_from_row(row) if row is not None else None

    def v2_position_plans(self) -> dict[str, V2PositionPlan]:
        rows = self._connection.execute(
            """SELECT symbol, cluster_id, setup_session, setups, quantity, entry_price,
                      stop_price, planned_heat, ma50, signal_close, opened_at,
                      exit_pending_reason, exit_triggered_at
               FROM paper_v2_position_plans WHERE portfolio_id=?""",
            (self._portfolio_id,),
        ).fetchall()
        plans = (_v2_plan_from_row(row) for row in rows)
        return {plan.symbol: plan for plan in plans}

    def mark_v2_exit_pending(
        self, symbol: str, *, reason: str, triggered_at: datetime
    ) -> None:
        if not reason.strip():
            raise ValueError("exit pending reason must not be empty")
        if triggered_at.tzinfo is None or triggered_at.utcoffset() is None:
            raise ValueError("exit pending time must include a timezone offset")
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE paper_v2_position_plans
                   SET exit_pending_reason=?, exit_triggered_at=?
                   WHERE portfolio_id=? AND symbol=?""",
                (reason, triggered_at.isoformat(), self._portfolio_id, symbol),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"v2 position plan not found: {symbol}")

    def delete_v2_position_plan(self, symbol: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM paper_v2_position_plans WHERE portfolio_id=? AND symbol=?",
                (self._portfolio_id, symbol),
            )


POSTGRES_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id UUID PRIMARY KEY,
    fill_sequence BIGSERIAL UNIQUE,
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
    signal_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    price NUMERIC NOT NULL CHECK (price > 0),
    notional NUMERIC NOT NULL CHECK (notional > 0),
    commission NUMERIC NOT NULL DEFAULT 0 CHECK (commission >= 0),
    tax NUMERIC NOT NULL DEFAULT 0 CHECK (tax >= 0),
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

POSTGRES_PORTFOLIO_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_portfolio_snapshots (
    snapshot_id UUID PRIMARY KEY,
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
    captured_at TIMESTAMPTZ NOT NULL,
    equity NUMERIC NOT NULL,
    realized_pnl NUMERIC NOT NULL,
    unrealized_pnl NUMERIC NOT NULL,
    total_costs NUMERIC NOT NULL,
    UNIQUE (portfolio_id, captured_at)
)
"""

POSTGRES_PORTFOLIO_DAILY_BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_portfolio_daily_baselines (
    portfolio_id TEXT NOT NULL DEFAULT 'legacy',
    trading_day DATE NOT NULL,
    equity NUMERIC NOT NULL,
    PRIMARY KEY (portfolio_id, trading_day)
)
"""

POSTGRES_V2_POSITION_PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_v2_position_plans (
    portfolio_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    setup_session DATE NOT NULL,
    setups JSONB NOT NULL,
    quantity NUMERIC NOT NULL CHECK (quantity > 0),
    entry_price NUMERIC NOT NULL CHECK (entry_price > 0),
    stop_price NUMERIC NOT NULL CHECK (stop_price > 0),
    planned_heat NUMERIC NOT NULL CHECK (planned_heat > 0),
    ma50 NUMERIC NOT NULL CHECK (ma50 > 0),
    signal_close NUMERIC NOT NULL CHECK (signal_close > 0),
    opened_at TIMESTAMPTZ NOT NULL,
    exit_pending_reason TEXT,
    exit_triggered_at TIMESTAMPTZ,
    PRIMARY KEY (portfolio_id, symbol),
    CHECK ((exit_pending_reason IS NULL) = (exit_triggered_at IS NULL))
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
                    "ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS fill_sequence BIGSERIAL"
                )
                cursor.execute(
                    "ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS commission NUMERIC NOT NULL DEFAULT 0"
                )
                cursor.execute(
                    "ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS tax NUMERIC NOT NULL DEFAULT 0"
                )
                cursor.execute(
                    "ALTER TABLE paper_risk_decisions ADD COLUMN IF NOT EXISTS portfolio_id TEXT NOT NULL DEFAULT 'legacy'"
                )
                cursor.execute(POSTGRES_AUTOMATION_RUN_SCHEMA)
                cursor.execute(POSTGRES_AUTOMATION_RUN_INDEX)
                cursor.execute(POSTGRES_PORTFOLIO_SCHEMA)
                cursor.execute(POSTGRES_PORTFOLIO_SNAPSHOT_SCHEMA)
                cursor.execute(POSTGRES_PORTFOLIO_DAILY_BASELINE_SCHEMA)
                cursor.execute(POSTGRES_V2_POSITION_PLAN_SCHEMA)
                cursor.execute(
                    "ALTER TABLE paper_v2_position_plans "
                    "ADD COLUMN IF NOT EXISTS cluster_id TEXT NOT NULL DEFAULT 'UNKNOWN'"
                )
                cursor.execute(
                    "ALTER TABLE paper_v2_position_plans "
                    "ADD COLUMN IF NOT EXISTS signal_close NUMERIC NOT NULL DEFAULT 1"
                )
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
        costs = self.estimate_costs(signal)
        fill = PaperFill(
            fill_id=str(uuid4()),
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.reference_price,
            notional=signal.notional,
            commission=costs.commission,
            tax=costs.tax,
            reason=signal.reason,
            executed_at=when,
        )
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO paper_fills (
                        fill_id, portfolio_id, signal_id, symbol, side, quantity, price,
                        notional, commission, tax, reason, executed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        fill.commission,
                        fill.tax,
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

    def estimate_costs(self, signal: TradeSignal) -> TradeCosts:
        return toss_trade_costs(signal)

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
        return self.position_accounting(symbol).cost_basis

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

    def position_accounting(self, symbol: str) -> PositionAccounting:
        return self.position_accountings().get(
            symbol,
            PositionAccounting(
                symbol=symbol,
                quantity=Decimal(0),
                cost_basis=Decimal(0),
                realized_pnl=Decimal(0),
                commission=Decimal(0),
                tax=Decimal(0),
            ),
        )

    def position_accountings(self) -> dict[str, PositionAccounting]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT symbol, side, quantity, notional, commission, tax
                FROM paper_fills
                WHERE portfolio_id = %s
                ORDER BY executed_at, fill_sequence
                """,
                (self._portfolio_id,),
            )
            rows = cursor.fetchall()
        return _position_accountings(rows)

    def daily_equity_baseline(self, captured_at: datetime) -> Decimal | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT equity FROM paper_portfolio_daily_baselines
                WHERE portfolio_id = %s AND trading_day = %s
                """,
                (self._portfolio_id, _utc_trading_day(captured_at)),
            )
            row = cursor.fetchone()
        return Decimal(row[0]) if row else None

    def record_daily_equity_baseline(
        self, *, captured_at: datetime, equity: Decimal
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO paper_portfolio_daily_baselines (
                    portfolio_id, trading_day, equity
                ) VALUES (%s, %s, %s)
                ON CONFLICT (portfolio_id, trading_day) DO NOTHING
                """,
                (self._portfolio_id, _utc_trading_day(captured_at), equity),
            )
        self._connection.commit()

    def record_portfolio_snapshot(
        self,
        *,
        captured_at: datetime,
        equity: Decimal,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        total_costs: Decimal,
    ) -> str:
        snapshot_id = str(uuid4())
        captured = _aware_utc(captured_at)
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO paper_portfolio_snapshots (
                    snapshot_id, portfolio_id, captured_at, equity,
                    realized_pnl, unrealized_pnl, total_costs
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (portfolio_id, captured_at) DO UPDATE SET
                    equity = EXCLUDED.equity,
                    realized_pnl = EXCLUDED.realized_pnl,
                    unrealized_pnl = EXCLUDED.unrealized_pnl,
                    total_costs = EXCLUDED.total_costs
                """,
                (
                    snapshot_id,
                    self._portfolio_id,
                    captured,
                    equity,
                    realized_pnl,
                    unrealized_pnl,
                    total_costs,
                ),
            )
        self._connection.commit()
        return snapshot_id

    def cash_balance(self, initial_cash: Decimal) -> Decimal:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN side = 'BUY'
                         THEN -notional - commission - tax
                         ELSE notional - commission - tax END
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

    def upsert_v2_position_plan(self, plan: V2PositionPlan) -> None:
        values = _v2_plan_values(self._portfolio_id, plan)
        with self._connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO paper_v2_position_plans (
                    portfolio_id, symbol, cluster_id, setup_session, setups, quantity,
                    entry_price, stop_price, planned_heat, ma50, signal_close,
                    opened_at, exit_pending_reason, exit_triggered_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (portfolio_id, symbol) DO UPDATE SET
                    cluster_id=excluded.cluster_id,
                    setup_session=excluded.setup_session,
                    setups=excluded.setups,
                    quantity=excluded.quantity,
                    entry_price=excluded.entry_price,
                    stop_price=excluded.stop_price,
                    planned_heat=excluded.planned_heat,
                    ma50=excluded.ma50,
                    signal_close=excluded.signal_close,
                    opened_at=excluded.opened_at,
                    exit_pending_reason=excluded.exit_pending_reason,
                    exit_triggered_at=excluded.exit_triggered_at""",
                values,
            )
        self._connection.commit()

    def v2_position_plan(self, symbol: str) -> V2PositionPlan | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """SELECT symbol, cluster_id, setup_session, setups, quantity, entry_price,
                          stop_price, planned_heat, ma50, signal_close, opened_at,
                          exit_pending_reason, exit_triggered_at
                   FROM paper_v2_position_plans
                   WHERE portfolio_id=%s AND symbol=%s""",
                (self._portfolio_id, symbol),
            )
            row = cursor.fetchone()
        return _v2_plan_from_row(row) if row is not None else None

    def v2_position_plans(self) -> dict[str, V2PositionPlan]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """SELECT symbol, cluster_id, setup_session, setups, quantity, entry_price,
                          stop_price, planned_heat, ma50, signal_close, opened_at,
                          exit_pending_reason, exit_triggered_at
                   FROM paper_v2_position_plans WHERE portfolio_id=%s""",
                (self._portfolio_id,),
            )
            rows = cursor.fetchall()
        plans = (_v2_plan_from_row(row) for row in rows)
        return {plan.symbol: plan for plan in plans}

    def mark_v2_exit_pending(
        self, symbol: str, *, reason: str, triggered_at: datetime
    ) -> None:
        if not reason.strip():
            raise ValueError("exit pending reason must not be empty")
        triggered = _aware_utc(triggered_at)
        with self._connection.cursor() as cursor:
            cursor.execute(
                """UPDATE paper_v2_position_plans
                   SET exit_pending_reason=%s, exit_triggered_at=%s
                   WHERE portfolio_id=%s AND symbol=%s""",
                (reason, triggered, self._portfolio_id, symbol),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                raise KeyError(f"v2 position plan not found: {symbol}")
        self._connection.commit()

    def delete_v2_position_plan(self, symbol: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM paper_v2_position_plans WHERE portfolio_id=%s AND symbol=%s",
                (self._portfolio_id, symbol),
            )
        self._connection.commit()


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


def _position_accountings(
    rows: Sequence[Sequence[object]],
) -> dict[str, PositionAccounting]:
    state: dict[str, list[Decimal]] = {}
    for raw_symbol, raw_side, raw_quantity, raw_notional, raw_commission, raw_tax in rows:
        symbol = str(raw_symbol)
        side = Side(str(raw_side))
        quantity = Decimal(raw_quantity)
        notional = Decimal(raw_notional)
        commission = Decimal(raw_commission)
        tax = Decimal(raw_tax)
        values = state.setdefault(symbol, [Decimal(0)] * 5)
        held, basis, realized, commissions, taxes = values
        commissions += commission
        taxes += tax
        if side is Side.BUY:
            held += quantity
            basis += notional + commission + tax
        else:
            if quantity > held:
                raise ValueError(f"paper fills oversell position: {symbol}")
            allocated_basis = basis * quantity / held
            held -= quantity
            basis -= allocated_basis
            realized += notional - commission - tax - allocated_basis
            if held == 0:
                basis = Decimal(0)
        state[symbol] = [held, basis, realized, commissions, taxes]
    return {
        symbol: PositionAccounting(
            symbol=symbol,
            quantity=values[0],
            cost_basis=values[1],
            realized_pnl=values[2],
            commission=values[3],
            tax=values[4],
        )
        for symbol, values in state.items()
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("portfolio snapshot time must include a timezone offset")
    return value.astimezone(UTC)


def _utc_trading_day(value: datetime) -> date:
    return _aware_utc(value).date()


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


def _v2_plan_values(
    portfolio_id: str, plan: V2PositionPlan, *, serialize: bool = False
) -> tuple[object, ...]:
    values: tuple[object, ...] = (
        portfolio_id,
        plan.symbol,
        plan.cluster_id,
        plan.setup_session,
        json.dumps(plan.setups, ensure_ascii=False),
        plan.quantity,
        plan.entry_price,
        plan.stop_price,
        plan.planned_heat,
        plan.ma50,
        plan.signal_close,
        plan.opened_at,
        plan.exit_pending_reason,
        plan.exit_triggered_at,
    )
    if not serialize:
        return values
    return tuple(
        value.isoformat()
        if isinstance(value, (date, datetime))
        else str(value)
        if isinstance(value, Decimal)
        else value
        for value in values
    )


def _v2_plan_from_row(row: Sequence[object]) -> V2PositionPlan:
    values = tuple(row)
    raw_setups = values[3]
    setups = (
        tuple(json.loads(raw_setups))
        if isinstance(raw_setups, str)
        else tuple(raw_setups)
    )
    setup_session = (
        values[2]
        if isinstance(values[2], date)
        else date.fromisoformat(str(values[2]))
    )
    opened_at = (
        values[10]
        if isinstance(values[10], datetime)
        else datetime.fromisoformat(str(values[10]))
    )
    triggered_at = (
        values[12]
        if isinstance(values[12], datetime)
        else datetime.fromisoformat(str(values[12]))
        if values[12] is not None
        else None
    )
    return V2PositionPlan(
        symbol=str(values[0]),
        cluster_id=str(values[1]),
        setup_session=setup_session,
        setups=tuple(str(value) for value in setups),
        quantity=Decimal(str(values[4])),
        entry_price=Decimal(str(values[5])),
        stop_price=Decimal(str(values[6])),
        planned_heat=Decimal(str(values[7])),
        ma50=Decimal(str(values[8])),
        signal_close=Decimal(str(values[9])),
        opened_at=opened_at,
        exit_pending_reason=(str(values[11]) if values[11] is not None else None),
        exit_triggered_at=triggered_at,
    )


def _serialized_datetime(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)
