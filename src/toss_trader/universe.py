from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .market_data import MarketCollector
from .models import Candle
from .repository import MarketRepository
from .risk import (
    RiskDecision,
    RiskManager,
    UniverseCandidateRisk,
    UniverseRiskContext,
)
from .setup_screening import evaluate_price_setups

SEOUL = ZoneInfo("Asia/Seoul")
RANKING_SOURCE_TOSS = "toss:realtime"
RANKING_SOURCE_KRX = "krx:acc-trdval"
KRX_AFTERNOON_START = time(12, 0)
STATIC_UNIVERSE_VIOLATIONS = frozenset(
    {
        "unsupported-security-type",
        "not-common-share",
        "stock-not-active",
        "trading-suspended",
        "invalid-reference-price",
    }
)


@dataclass(frozen=True, slots=True)
class UniverseDecision:
    symbol: str
    score: Decimal
    amount_rank: int | None
    gainer_rank: int | None
    eligible_rank: int | None
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
    def latest_selected_between(
        self,
        since: datetime,
        until: datetime,
        *,
        ranking_source: str = RANKING_SOURCE_TOSS,
    ) -> tuple[str, ...] | None: ...

    def record_success(
        self,
        *,
        run_id: str,
        evaluated_at: datetime,
        ranked_at: datetime | None,
        ranking_source: str,
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
    ranking_source TEXT,
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
    eligible_rank INTEGER,
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
    ranking_source TEXT,
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
    eligible_rank INTEGER,
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
;
ALTER TABLE dynamic_universe_decisions
ADD COLUMN IF NOT EXISTS eligible_rank INTEGER;
ALTER TABLE dynamic_universe_runs
ADD COLUMN IF NOT EXISTS ranking_source TEXT
"""


class DynamicUniverseSelector:
    def __init__(
        self,
        *,
        client: RankingClient,
        collector: MarketCollector,
        repository: MarketRepository,
        store: UniverseStore,
        risk_manager: RiskManager,
        candidate_count: int,
        ranking_fetch_count: int,
        universe_size: int,
        krx_amount_rankings: Callable[[datetime, int], dict[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._collector = collector
        self._repository = repository
        self._store = store
        self._risk_manager = risk_manager
        self._candidate_count = candidate_count
        self._ranking_fetch_count = ranking_fetch_count
        self._universe_size = universe_size
        self._krx_amount_rankings = krx_amount_rankings
        if ranking_fetch_count < candidate_count:
            raise ValueError("ranking fetch count must be at least candidate count")
        if universe_size > candidate_count:
            raise ValueError("universe size must not exceed candidate count")

    def resolve(
        self,
        *,
        now: datetime,
        held_symbols: tuple[str, ...],
        risk_context: UniverseRiskContext,
    ) -> UniverseRefreshResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("universe decision time must include a timezone offset")
        source = ranking_source_for(now)
        cached = self._store.latest_selected_between(
            _seoul_day_start(now), now, ranking_source=source
        )
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
            self._store.record_success(
                run_id=run_id,
                evaluated_at=now,
                ranked_at=ranked_at,
                ranking_source=source,
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
        amount = self._amount_payload(now)
        amount_rows, amount_ranked_at = _ranking_rows(amount)
        ranked = _amount_rankings(amount_rows)
        if not ranked:
            raise RuntimeError("dynamic universe rankings are empty")
        symbols = tuple(item["symbol"] for item in ranked)
        stocks = self._client.stocks(symbols)
        stock_by_symbol = _stock_info(stocks, symbols)
        self._repository.upsert_symbol_names(
            {symbol: str(stock_by_symbol[symbol]["name"]) for symbol in symbols}
        )
        provisional: list[tuple[dict[str, Any], RiskDecision]] = []
        eligible_rank = 0
        for item in ranked:
            symbol = item["symbol"]
            stock = stock_by_symbol[symbol]
            security_type, is_common_share, status, trading_suspended = (
                _stock_risk_fields(stock, symbol)
            )
            static_risk = self._risk_manager.evaluate_universe_candidate(
                UniverseCandidateRisk(
                    symbol=symbol,
                    reference_price=item["reference_price"],
                    security_type=security_type,
                    is_common_share=is_common_share,
                    status=status,
                    trading_suspended=trading_suspended,
                ),
                risk_context,
            )
            unexpected_risk = set(static_risk.violations) - STATIC_UNIVERSE_VIOLATIONS
            if unexpected_risk:
                raise RuntimeError(
                    f"universe risk policy unavailable for {symbol}: "
                    f"{','.join(sorted(unexpected_risk))}"
                )
            if not static_risk.approved:
                provisional.append((item, static_risk))
                continue
            eligible_rank += 1
            item["eligible_rank"] = eligible_rank
            if eligible_rank > self._candidate_count:
                provisional.append(
                    (
                        item,
                        RiskDecision(
                            approved=False,
                            violations=("outside-eligible-candidate-limit",),
                        ),
                    )
                )
                continue
            try:
                completed = self._completed_daily(symbol, now=now)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"universe price data unavailable for {symbol}: "
                    f"{type(error).__name__}"
                ) from error
            if len(completed) < 200:
                provisional.append(
                    (
                        item,
                        RiskDecision(
                            approved=False,
                            violations=(
                                f"completed-daily-candles({len(completed)}/200)",
                            ),
                        ),
                    )
                )
                continue
            try:
                price = evaluate_price_setups(completed[-200:])
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"universe price setup invalid for {symbol}: "
                    f"{type(error).__name__}"
                ) from error
            if not price.setups:
                provisional.append(
                    (
                        item,
                        RiskDecision(True, ("missing-price-setup",)),
                    )
                )
                continue
            provisional.append((item, static_risk))
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
                eligible_rank=item["eligible_rank"],
                change_rate=item["change_rate"],
                trading_amount=item["trading_amount"],
                reference_price=item["reference_price"],
                risk=risk,
                selected=item["symbol"] in selected_symbols,
            )
            for item, risk in provisional
        )
        return decisions, amount_ranked_at

    def _amount_payload(self, now: datetime) -> dict[str, Any]:
        if ranking_source_for(now) == RANKING_SOURCE_KRX:
            if self._krx_amount_rankings is None:
                raise RuntimeError(
                    "KRX_API_KEY is required for afternoon universe rankings"
                )
            return self._krx_amount_rankings(now, self._ranking_fetch_count)
        return self._client.rankings(
            ranking_type="MARKET_TRADING_AMOUNT",
            market_country="KR",
            duration="realtime",
            exclude_investment_caution=True,
            count=self._ranking_fetch_count,
        )

    def _completed_daily(self, symbol: str, *, now: datetime) -> list[Candle]:
        today = now.astimezone(SEOUL).date()
        completed = self._stored_completed_daily(symbol, today=today)
        if len(completed) >= 200:
            return completed
        before: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(5):
            completed_before = len(completed)
            kwargs: dict[str, object] = {
                "symbol": symbol,
                "interval": "1d",
                "count": max(1, 200 - len(completed)),
            }
            if before is not None:
                kwargs["before"] = before
            collection = self._collector.collect(**kwargs)
            if (
                not isinstance(collection.received, int)
                or isinstance(collection.received, bool)
                or collection.received < 0
                or collection.received > kwargs["count"]
            ):
                raise RuntimeError(f"invalid daily collection count for {symbol}")
            completed = self._stored_completed_daily(symbol, today=today)
            if len(completed) >= 200:
                return completed
            if collection.next_before is None:
                if completed and len(completed) == completed_before:
                    raise RuntimeError(
                        f"daily history made no progress before exhaustion for {symbol}"
                    )
                return completed
            if collection.next_before in seen_cursors:
                raise RuntimeError(f"daily cursor made no progress for {symbol}")
            seen_cursors.add(collection.next_before)
            before = collection.next_before
        raise RuntimeError(f"daily pagination limit reached for {symbol}")

    def _stored_completed_daily(self, symbol: str, *, today: date) -> list[Candle]:
        candles = self._repository.latest_candles(symbol, "1d", limit=400)
        return [
            candle
            for candle in candles
            if candle.timestamp.astimezone(SEOUL).date() < today
        ]


class SqliteUniverseStore:
    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.executescript(SQLITE_SCHEMA)
        columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(dynamic_universe_decisions)"
            )
        }
        if "eligible_rank" not in columns:
            self._connection.execute(
                "ALTER TABLE dynamic_universe_decisions "
                "ADD COLUMN eligible_rank INTEGER"
            )
        run_columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(dynamic_universe_runs)"
            )
        }
        if "ranking_source" not in run_columns:
            self._connection.execute(
                "ALTER TABLE dynamic_universe_runs ADD COLUMN ranking_source TEXT"
            )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def latest_selected_between(
        self,
        since: datetime,
        until: datetime,
        *,
        ranking_source: str = RANKING_SOURCE_TOSS,
    ) -> tuple[str, ...] | None:
        row = self._connection.execute(
            """
            SELECT run_id FROM dynamic_universe_runs
            WHERE status = 'succeeded' AND selected_count > 0
              AND evaluated_at >= ? AND evaluated_at <= ?
              AND COALESCE(ranking_source, ?) = ?
            ORDER BY evaluated_at DESC LIMIT 1
            """,
            (
                since.isoformat(),
                until.isoformat(),
                RANKING_SOURCE_TOSS,
                ranking_source,
            ),
        ).fetchone()
        if row is None:
            return None
        rows = self._connection.execute(
            """
            SELECT symbol FROM dynamic_universe_decisions
            WHERE run_id = ? AND selected = 1
            ORDER BY
                CASE WHEN eligible_rank IS NULL THEN score END DESC,
                eligible_rank,
                amount_rank,
                symbol
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
        ranking_source: str,
        decisions: Sequence[UniverseDecision],
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO dynamic_universe_runs
                (run_id, evaluated_at, ranked_at, status, candidate_count,
                 approved_count, selected_count, ranking_source, error_message)
                VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    evaluated_at.isoformat(),
                    ranked_at.isoformat() if ranked_at else None,
                    len(decisions),
                    sum(item.risk.approved for item in decisions),
                    sum(item.selected for item in decisions),
                    ranking_source,
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO dynamic_universe_decisions
                (decision_id, run_id, evaluated_at, symbol, score, amount_rank,
                 gainer_rank, eligible_rank, change_rate, trading_amount,
                 reference_price, risk_approved, selected, violations)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_sqlite_decision(run_id, evaluated_at, item) for item in decisions],
            )

    def record_failure(
        self, *, run_id: str, evaluated_at: datetime, error_message: str
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO dynamic_universe_runs
                (run_id, evaluated_at, ranked_at, status, candidate_count,
                 approved_count, selected_count, error_message) VALUES
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

    def latest_selected_between(
        self,
        since: datetime,
        until: datetime,
        *,
        ranking_source: str = RANKING_SOURCE_TOSS,
    ) -> tuple[str, ...] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id FROM dynamic_universe_runs
                WHERE status = 'succeeded'
                  AND selected_count > 0
                  AND evaluated_at >= %s AND evaluated_at <= %s
                  AND COALESCE(ranking_source, %s) = %s
                ORDER BY evaluated_at DESC LIMIT 1
                """,
                (since, until, RANKING_SOURCE_TOSS, ranking_source),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT symbol FROM dynamic_universe_decisions
                WHERE run_id = %s AND selected
                ORDER BY
                    CASE WHEN eligible_rank IS NULL THEN score END DESC,
                    eligible_rank,
                    amount_rank,
                    symbol
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
        ranking_source: str,
        decisions: Sequence[UniverseDecision],
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO dynamic_universe_runs
                (run_id, evaluated_at, ranked_at, status, candidate_count,
                 approved_count, selected_count, ranking_source, error_message)
                VALUES
                (%s, %s, %s, 'succeeded', %s, %s, %s, %s, NULL)
                """,
                (
                    run_id,
                    evaluated_at,
                    ranked_at,
                    len(decisions),
                    sum(item.risk.approved for item in decisions),
                    sum(item.selected for item in decisions),
                    ranking_source,
                ),
            )
            cursor.executemany(
                """
                INSERT INTO dynamic_universe_decisions
                (decision_id, run_id, evaluated_at, symbol, score, amount_rank,
                 gainer_rank, eligible_rank, change_rate, trading_amount,
                 reference_price, risk_approved, selected, violations)
                VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                INSERT INTO dynamic_universe_runs
                (run_id, evaluated_at, ranked_at, status, candidate_count,
                 approved_count, selected_count, error_message) VALUES
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
    ranked_at = None
    if isinstance(ranked_at_raw, str):
        ranked_at = datetime.fromisoformat(ranked_at_raw)
        if ranked_at.tzinfo is None or ranked_at.utcoffset() is None:
            raise ValueError("ranking rankedAt must include a timezone offset")
    elif ranked_at_raw is not None:
        raise TypeError("ranking rankedAt must be text or null")
    if any(not isinstance(item, Mapping) for item in rankings):
        raise TypeError("each ranking must be an object")
    return rankings, ranked_at


def _amount_rankings(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    symbols: set[str] = set()
    ranks: set[int] = set()
    for item in rows:
        symbol = item.get("symbol")
        rank = item.get("rank")
        if not isinstance(symbol, str) or not symbol:
            raise TypeError("ranking item missing symbol")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            raise TypeError(f"ranking item has invalid rank for {symbol}")
        if symbol in symbols or rank in ranks:
            raise ValueError("ranking response contains duplicate symbol or rank")
        symbols.add(symbol)
        ranks.add(rank)
        price = item.get("price")
        if not isinstance(price, Mapping):
            raise TypeError(f"ranking price missing for {symbol}")
        ranked.append(
            {
                "symbol": symbol,
                "score": Decimal(1) / Decimal(rank),
                "amount_rank": rank,
                "gainer_rank": None,
                "eligible_rank": None,
                "change_rate": _required_decimal(
                    price, "changeRate", context=f"ranking price for {symbol}"
                ),
                "trading_amount": _required_decimal(
                    item, "tradingAmount", context=f"ranking item for {symbol}"
                ),
                "reference_price": _required_decimal(
                    price, "lastPrice", context=f"ranking price for {symbol}"
                ),
            }
        )
    return sorted(
        ranked,
        key=lambda item: (
            item["amount_rank"],
            -item["trading_amount"],
            item["symbol"],
        ),
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
        if symbol in result:
            raise ValueError(f"stock response contains duplicate symbol: {symbol}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"stock name missing for {symbol}")
        _stock_risk_fields(item, symbol)
        result[symbol] = item
    missing = sorted(set(requested) - result.keys())
    if missing:
        raise ValueError(f"stock info missing symbols: {', '.join(missing)}")
    return result


def _stock_risk_fields(
    stock: Mapping[str, Any], symbol: str
) -> tuple[str, bool, str, bool]:
    security_type = stock.get("securityType")
    is_common_share = stock.get("isCommonShare")
    status = stock.get("status")
    korean_market_detail = stock.get("koreanMarketDetail")
    if not isinstance(security_type, str) or not security_type:
        raise TypeError(f"stock securityType missing for {symbol}")
    if not isinstance(is_common_share, bool):
        raise TypeError(f"stock isCommonShare missing for {symbol}")
    if not isinstance(status, str) or not status:
        raise TypeError(f"stock status missing for {symbol}")
    if not isinstance(korean_market_detail, Mapping):
        raise TypeError(f"stock koreanMarketDetail missing for {symbol}")
    krx_suspended = korean_market_detail.get("krxTradingSuspended")
    nxt_suspended = korean_market_detail.get("nxtTradingSuspended")
    if not isinstance(krx_suspended, bool) or not isinstance(nxt_suspended, bool):
        raise TypeError(f"stock suspension status missing for {symbol}")
    return security_type, is_common_share, status, krx_suspended or nxt_suspended


def _with_held(
    selected: tuple[str, ...], held_symbols: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*selected, *held_symbols)))


def ranking_source_for(now: datetime) -> str:
    if now.astimezone(SEOUL).time() >= KRX_AFTERNOON_START:
        return RANKING_SOURCE_KRX
    return RANKING_SOURCE_TOSS


def _seoul_day_start(now: datetime) -> datetime:
    local_date = now.astimezone(SEOUL).date()
    return datetime.combine(local_date, time.min, tzinfo=SEOUL).astimezone(UTC)


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid ranking decimal: {value}") from error
    if not result.is_finite():
        raise ValueError(f"invalid ranking decimal: {value}")
    return result


def _required_decimal(
    payload: Mapping[str, Any], field: str, *, context: str
) -> Decimal:
    if field not in payload:
        raise ValueError(f"{context} missing {field}")
    result = _decimal(payload[field])
    if field == "tradingAmount" and result < 0:
        raise ValueError(f"{context} has negative {field}")
    return result


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
        item.eligible_rank,
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
        item.eligible_rank,
        item.change_rate,
        item.trading_amount,
        item.reference_price,
        item.risk.approved,
        item.selected,
        json.dumps(item.risk.violations),
    )
