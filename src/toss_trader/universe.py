from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .repository import MarketRepository
from .risk import (
    RiskDecision,
    RiskManager,
    UniverseCandidateRisk,
    UniverseRiskContext,
)


@dataclass(frozen=True, slots=True)
class UniverseDecision:
    symbol: str
    score: Decimal
    amount_rank: int | None
    gainer_rank: int | None
    change_rate: Decimal
    trading_amount: Decimal
    reference_price: Decimal
    risk: RiskDecision
    selected: bool


@dataclass(frozen=True, slots=True)
class UniverseRefreshResult:
    run_id: str | None
    refreshed: bool
    symbols: tuple[str, ...]
    new_buys_allowed: bool
    entry_symbols: tuple[str, ...] = ()


class RankingClient(Protocol):
    def rankings(
        self,
        *,
        ranking_type: str,
        market_country: str,
        duration: str,
        exclude_investment_caution: bool,
        count: int,
    ) -> dict[str, Any]: ...

    def stocks(self, symbols: Sequence[str]) -> list[dict[str, Any]]: ...


class UniverseStore(Protocol):
    def latest_selected_since(self, since: datetime) -> tuple[str, ...] | None: ...

    def record_success(
        self,
        *,
        run_id: str,
        evaluated_at: datetime,
        ranked_at: datetime | None,
        decisions: Sequence[UniverseDecision],
    ) -> None: ...

    def record_failure(
        self, *, run_id: str, evaluated_at: datetime, error_message: str
    ) -> None: ...

    def close(self) -> None: ...


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dynamic_universe_runs (
    run_id TEXT PRIMARY KEY,
    evaluated_at TEXT NOT NULL,
    ranked_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    candidate_count INTEGER NOT NULL,
    approved_count INTEGER NOT NULL,
    selected_count INTEGER NOT NULL,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS dynamic_universe_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES dynamic_universe_runs(run_id),
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
);
CREATE INDEX IF NOT EXISTS dynamic_universe_runs_time_idx
ON dynamic_universe_runs (evaluated_at DESC);
CREATE INDEX IF NOT EXISTS dynamic_universe_decisions_run_idx
ON dynamic_universe_decisions (run_id, selected, score DESC)
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS dynamic_universe_runs (
    run_id UUID PRIMARY KEY,
    evaluated_at TIMESTAMPTZ NOT NULL,
    ranked_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    candidate_count INTEGER NOT NULL,
    approved_count INTEGER NOT NULL,
    selected_count INTEGER NOT NULL,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS dynamic_universe_decisions (
    decision_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES dynamic_universe_runs(run_id),
    evaluated_at TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    score NUMERIC NOT NULL,
    amount_rank INTEGER,
    gainer_rank INTEGER,
    change_rate NUMERIC NOT NULL,
    trading_amount NUMERIC NOT NULL,
    reference_price NUMERIC NOT NULL,
    risk_approved BOOLEAN NOT NULL,
    selected BOOLEAN NOT NULL,
    violations JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS dynamic_universe_runs_time_idx
ON dynamic_universe_runs (evaluated_at DESC);
CREATE INDEX IF NOT EXISTS dynamic_universe_decisions_run_idx
ON dynamic_universe_decisions (run_id, selected, score DESC)
"""


class DynamicUniverseSelector:
    def __init__(
        self,
        *,
        client: RankingClient,
        repository: MarketRepository,
        store: UniverseStore,
        risk_manager: RiskManager,
        refresh_interval: timedelta,
        candidate_count: int,
        universe_size: int,
    ) -> None:
        self._client = client
        self._repository = repository
        self._store = store
        self._risk_manager = risk_manager
        self._refresh_interval = refresh_interval
        self._candidate_count = candidate_count
        self._universe_size = universe_size

    def resolve(
        self,
        *,
        now: datetime,
        held_symbols: tuple[str, ...],
        risk_context: UniverseRiskContext,
    ) -> UniverseRefreshResult:
        cached = self._store.latest_selected_since(now - self._refresh_interval)
        if cached is not None:
            return UniverseRefreshResult(
                run_id=None,
                refreshed=False,
                symbols=_with_held(cached, held_symbols),
                new_buys_allowed=True,
                entry_symbols=(),
            )
        run_id = str(uuid4())
        try:
            decisions, ranked_at = self._refresh(now, risk_context)
            selected = tuple(item.symbol for item in decisions if item.selected)
            if not selected:
                raise RuntimeError("RiskManager rejected every dynamic universe candidate")
            self._store.record_success(
                run_id=run_id,
                evaluated_at=now,
                ranked_at=ranked_at,
                decisions=decisions,
            )
            return UniverseRefreshResult(
                run_id=run_id,
                refreshed=True,
                symbols=_with_held(selected, held_symbols),
                new_buys_allowed=True,
                entry_symbols=selected,
            )
        except Exception as error:
            self._store.record_failure(
                run_id=run_id, evaluated_at=now, error_message=str(error)
            )
            if held_symbols:
                return UniverseRefreshResult(
                    run_id=run_id,
                    refreshed=True,
                    symbols=tuple(dict.fromkeys(held_symbols)),
                    new_buys_allowed=False,
                    entry_symbols=(),
                )
            raise

    def _refresh(
        self, now: datetime, risk_context: UniverseRiskContext
    ) -> tuple[tuple[UniverseDecision, ...], datetime | None]:
        amount = self._client.rankings(
            ranking_type="MARKET_TRADING_AMOUNT",
            market_country="KR",
            duration="realtime",
            exclude_investment_caution=True,
            count=self._candidate_count,
        )
        gainers = self._client.rankings(
            ranking_type="TOP_GAINERS",
            market_country="KR",
            duration="1d",
            exclude_investment_caution=True,
            count=self._candidate_count,
        )
        amount_rows, amount_ranked_at = _ranking_rows(amount)
        gainer_rows, gainer_ranked_at = _ranking_rows(gainers)
        combined = _combine_rankings(amount_rows, gainer_rows, self._candidate_count)
        if not combined:
            raise RuntimeError("dynamic universe rankings are empty")
        symbols = tuple(item["symbol"] for item in combined)
        stocks = self._client.stocks(symbols)
        stock_by_symbol = _stock_info(stocks, symbols)
        self._repository.upsert_symbol_names(
            {symbol: str(stock_by_symbol[symbol]["name"]) for symbol in symbols}
        )
        provisional: list[tuple[dict[str, Any], RiskDecision]] = []
        for item in combined:
            symbol = item["symbol"]
            stock = stock_by_symbol[symbol]
            kr_detail = stock.get("koreanMarketDetail")
            kr_detail = kr_detail if isinstance(kr_detail, Mapping) else {}
            risk = self._risk_manager.evaluate_universe_candidate(
                UniverseCandidateRisk(
                    symbol=symbol,
                    reference_price=item["reference_price"],
                    security_type=str(stock.get("securityType", "")),
                    is_common_share=stock.get("isCommonShare") is True,
                    status=str(stock.get("status", "")),
                    trading_suspended=(
                        kr_detail.get("krxTradingSuspended") is True
                        or kr_detail.get("nxtTradingSuspended") is True
                    ),
                ),
                risk_context,
            )
            provisional.append((item, risk))
        selected_symbols = set(
            [
                item["symbol"]
                for item, risk in provisional
                if risk.approved
            ][: self._universe_size]
        )
        decisions = tuple(
            UniverseDecision(
                symbol=item["symbol"],
                score=item["score"],
                amount_rank=item["amount_rank"],
                gainer_rank=item["gainer_rank"],
                change_rate=item["change_rate"],
                trading_amount=item["trading_amount"],
                reference_price=item["reference_price"],
                risk=risk,
                selected=item["symbol"] in selected_symbols,
            )
            for item, risk in provisional
        )
        ranked_at = max(
            (value for value in (amount_ranked_at, gainer_ranked_at) if value),
            default=now,
        )
        return decisions, ranked_at


class SqliteUniverseStore:
    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.executescript(SQLITE_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def latest_selected_since(self, since: datetime) -> tuple[str, ...] | None:
        row = self._connection.execute(
            """
            SELECT run_id FROM dynamic_universe_runs
            WHERE status = 'succeeded' AND evaluated_at >= ?
            ORDER BY evaluated_at DESC LIMIT 1
            """,
            (since.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        rows = self._connection.execute(
            """
            SELECT symbol FROM dynamic_universe_decisions
            WHERE run_id = ? AND selected = 1 ORDER BY score DESC, symbol
            """,
            (row[0],),
        ).fetchall()
        return tuple(str(item[0]) for item in rows)

    def record_success(
        self,
        *,
        run_id: str,
        evaluated_at: datetime,
        ranked_at: datetime | None,
        decisions: Sequence[UniverseDecision],
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO dynamic_universe_runs VALUES (?, ?, ?, 'succeeded', ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    evaluated_at.isoformat(),
                    ranked_at.isoformat() if ranked_at else None,
                    len(decisions),
                    sum(item.risk.approved for item in decisions),
                    sum(item.selected for item in decisions),
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO dynamic_universe_decisions VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_sqlite_decision(run_id, evaluated_at, item) for item in decisions],
            )

    def record_failure(
        self, *, run_id: str, evaluated_at: datetime, error_message: str
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO dynamic_universe_runs VALUES
                (?, ?, NULL, 'failed', 0, 0, 0, ?)
                """,
                (run_id, evaluated_at.isoformat(), error_message[:2000]),
            )


class PostgresUniverseStore:
    def __init__(self, connection_parameters: Mapping[str, str | int]) -> None:
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL support requires: pip install 'toss-trader[postgres]'"
            ) from error
        self._connection = psycopg.connect(**connection_parameters)
        with self._connection.cursor() as cursor:
            for statement in POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def latest_selected_since(self, since: datetime) -> tuple[str, ...] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id FROM dynamic_universe_runs
                WHERE status = 'succeeded' AND evaluated_at >= %s
                ORDER BY evaluated_at DESC LIMIT 1
                """,
                (since,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT symbol FROM dynamic_universe_decisions
                WHERE run_id = %s AND selected ORDER BY score DESC, symbol
                """,
                (row[0],),
            )
            return tuple(str(item[0]) for item in cursor.fetchall())

    def record_success(
        self,
        *,
        run_id: str,
        evaluated_at: datetime,
        ranked_at: datetime | None,
        decisions: Sequence[UniverseDecision],
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO dynamic_universe_runs VALUES
                (%s, %s, %s, 'succeeded', %s, %s, %s, NULL)
                """,
                (
                    run_id,
                    evaluated_at,
                    ranked_at,
                    len(decisions),
                    sum(item.risk.approved for item in decisions),
                    sum(item.selected for item in decisions),
                ),
            )
            cursor.executemany(
                """
                INSERT INTO dynamic_universe_decisions VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [_postgres_decision(run_id, evaluated_at, item) for item in decisions],
            )
        self._connection.commit()

    def record_failure(
        self, *, run_id: str, evaluated_at: datetime, error_message: str
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO dynamic_universe_runs VALUES
                (%s, %s, NULL, 'failed', 0, 0, 0, %s)
                """,
                (run_id, evaluated_at, error_message[:2000]),
            )
        self._connection.commit()


def open_universe_store(
    *, postgres_parameters: Mapping[str, str | int] | None, sqlite_path: str
) -> UniverseStore:
    if postgres_parameters:
        return PostgresUniverseStore(postgres_parameters)
    return SqliteUniverseStore(sqlite_path)


def _ranking_rows(payload: object) -> tuple[list[Mapping[str, Any]], datetime | None]:
    if not isinstance(payload, Mapping):
        raise TypeError("ranking response must be an object")
    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        raise TypeError("ranking response must contain rankings")
    ranked_at_raw = payload.get("rankedAt")
    ranked_at = (
        datetime.fromisoformat(ranked_at_raw)
        if isinstance(ranked_at_raw, str)
        else None
    )
    if any(not isinstance(item, Mapping) for item in rankings):
        raise TypeError("each ranking must be an object")
    return rankings, ranked_at


def _combine_rankings(
    amount_rows: Sequence[Mapping[str, Any]],
    gainer_rows: Sequence[Mapping[str, Any]],
    candidate_count: int,
) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for source, rows in (("amount", amount_rows), ("gainer", gainer_rows)):
        for item in rows:
            symbol = item.get("symbol")
            rank = item.get("rank")
            if not isinstance(symbol, str) or not isinstance(rank, int):
                raise TypeError("ranking item missing symbol or rank")
            price = item.get("price")
            if not isinstance(price, Mapping):
                raise TypeError(f"ranking price missing for {symbol}")
            entry = combined.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "amount_rank": None,
                    "gainer_rank": None,
                    "change_rate": Decimal(0),
                    "trading_amount": Decimal(0),
                    "reference_price": Decimal(0),
                },
            )
            entry[f"{source}_rank"] = rank
            entry["change_rate"] = _decimal(price.get("changeRate", 0))
            entry["reference_price"] = _decimal(price.get("lastPrice", 0))
            entry["trading_amount"] = max(
                entry["trading_amount"], _decimal(item.get("tradingAmount", 0))
            )
    for item in combined.values():
        amount_score = (
            2 * (candidate_count + 1 - item["amount_rank"])
            if item["amount_rank"] is not None
            else 0
        )
        gainer_score = (
            candidate_count + 1 - item["gainer_rank"]
            if item["gainer_rank"] is not None
            else 0
        )
        item["score"] = Decimal(amount_score + gainer_score)
    return sorted(
        combined.values(),
        key=lambda item: (-item["score"], -item["trading_amount"], item["symbol"]),
    )


def _stock_info(
    payload: object, requested: tuple[str, ...]
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise TypeError("stocks response must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            raise TypeError("each stock must be an object")
        symbol = item.get("symbol")
        name = item.get("name")
        if not isinstance(symbol, str) or symbol not in requested:
            raise ValueError("stock response contains invalid symbol")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"stock name missing for {symbol}")
        result[symbol] = item
    missing = sorted(set(requested) - result.keys())
    if missing:
        raise ValueError(f"stock info missing symbols: {', '.join(missing)}")
    return result


def _with_held(
    selected: tuple[str, ...], held_symbols: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*selected, *held_symbols)))


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid ranking decimal: {value}") from error


def _sqlite_decision(
    run_id: str, evaluated_at: datetime, item: UniverseDecision
) -> tuple[Any, ...]:
    return (
        str(uuid4()),
        run_id,
        evaluated_at.isoformat(),
        item.symbol,
        str(item.score),
        item.amount_rank,
        item.gainer_rank,
        str(item.change_rate),
        str(item.trading_amount),
        str(item.reference_price),
        int(item.risk.approved),
        int(item.selected),
        json.dumps(item.risk.violations),
    )


def _postgres_decision(
    run_id: str, evaluated_at: datetime, item: UniverseDecision
) -> tuple[Any, ...]:
    return (
        str(uuid4()),
        run_id,
        evaluated_at,
        item.symbol,
        item.score,
        item.amount_rank,
        item.gainer_rank,
        item.change_rate,
        item.trading_amount,
        item.reference_price,
        item.risk.approved,
        item.selected,
        json.dumps(item.risk.violations),
    )
