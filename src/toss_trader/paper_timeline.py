from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .models import Side
from .paper import _position_accountings

SEOUL = ZoneInfo("Asia/Seoul")
PORTFOLIOS = ("rule", "hermes")


class PostgresPaperTimelineStore:
    def __init__(
        self,
        connection_parameters: Mapping[str, str | int],
        *,
        initial_cash: Decimal,
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
        self._connect = connect
        self._database_error = database_error or Exception
        self._parameters = {name: connection_parameters[name] for name in required}
        self._initial_cash = initial_cash

    def payload(self) -> dict[str, Any]:
        try:
            connection = self._connect(
                **self._parameters,
                application_name="toss-paper-timeline",
                connect_timeout=5,
                options=(
                    "-c default_transaction_read_only=on "
                    "-c statement_timeout=10000 "
                    "-c idle_in_transaction_session_timeout=10000"
                ),
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT portfolio_id, initial_cash
                        FROM paper_portfolios
                        WHERE portfolio_id IN ('rule', 'hermes')
                        """
                    )
                    initial_rows = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT portfolio_id, symbol, side, quantity, price,
                               notional, commission, tax, reason, executed_at
                        FROM paper_fills
                        WHERE portfolio_id IN ('rule', 'hermes')
                        ORDER BY executed_at, fill_sequence
                        """
                    )
                    fill_rows = cursor.fetchall()
                    symbols = sorted({str(row[1]) for row in fill_rows})
                    mark_rows: Sequence[Sequence[Any]] = ()
                    if symbols:
                        cursor.execute(
                            """
                            SELECT c.symbol, s.display_name, c.close_price,
                                   c.currency, c.timestamp
                            FROM market_candles AS c
                            LEFT JOIN market_symbols AS s ON s.symbol = c.symbol
                            WHERE c.symbol = ANY(%s)
                            ORDER BY c.timestamp, c.symbol,
                                     CASE c.interval WHEN '1m' THEN 1 ELSE 0 END
                            """,
                            (symbols,),
                        )
                        mark_rows = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT portfolio_id, status, interval, started_at
                        FROM paper_cycle_runs
                        WHERE portfolio_id IN ('rule', 'hermes')
                        ORDER BY started_at
                        """
                    )
                    cycle_rows = cursor.fetchall()
            finally:
                connection.close()
        except self._database_error as error:
            raise RuntimeError("PostgreSQL paper timeline query failed") from error
        return build_paper_timeline(
            initial_rows=initial_rows,
            fill_rows=fill_rows,
            mark_rows=mark_rows,
            cycle_rows=cycle_rows,
            default_initial_cash=self._initial_cash,
        )


def build_paper_timeline(
    *,
    initial_rows: Sequence[Sequence[Any]],
    fill_rows: Sequence[Sequence[Any]],
    mark_rows: Sequence[Sequence[Any]],
    cycle_rows: Sequence[Sequence[Any]],
    default_initial_cash: Decimal,
) -> dict[str, Any]:
    initial_cash = {
        str(portfolio_id): Decimal(value)
        for portfolio_id, value in initial_rows
        if str(portfolio_id) in PORTFOLIOS
    }
    fills = [_fill(row) for row in fill_rows if str(row[0]) in PORTFOLIOS]
    if not fills:
        raise ValueError("paper timeline requires Rule or Hermes fills")
    first_date = min(item["executedAt"].astimezone(SEOUL).date() for item in fills)
    daily_marks: dict[date, dict[str, dict[str, Any]]] = {}
    mark_history: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    currencies: set[str] = set()
    for row in mark_rows:
        timestamp = _datetime(row[4])
        trading_date = timestamp.astimezone(SEOUL).date()
        currency = str(row[3]).upper()
        currencies.add(currency)
        symbol = str(row[0])
        name = str(row[1]) if row[1] is not None else None
        if name:
            names[symbol] = name
        mark = {
            "symbol": str(row[0]),
            "name": name,
            "price": Decimal(row[2]),
            "currency": currency,
            "markedAt": timestamp,
        }
        mark_history.setdefault(symbol, []).append(mark)
        if trading_date >= first_date:
            daily_marks.setdefault(trading_date, {})[symbol] = mark
    cycle_dates = {
        _datetime(row[3]).astimezone(SEOUL).date()
        for row in cycle_rows
        if str(row[0]) in PORTFOLIOS
    }
    dates = sorted({*daily_marks, *cycle_dates, first_date})
    dates = [trading_date for trading_date in dates if trading_date >= first_date]
    if not dates:
        dates = [first_date]
    latest_marks = {
        symbol: max(
            (
                mark
                for mark in history
                if mark["markedAt"].astimezone(SEOUL).date() < first_date
            ),
            key=lambda mark: mark["markedAt"],
        )
        for symbol, history in mark_history.items()
        if any(
            mark["markedAt"].astimezone(SEOUL).date() < first_date for mark in history
        )
    }
    portfolios = {
        portfolio_id: _portfolio_days(
            portfolio_id=portfolio_id,
            dates=dates,
            fills=fills,
            cycle_rows=cycle_rows,
            daily_marks=daily_marks,
            latest_marks=latest_marks,
            mark_history=mark_history,
            names=names,
            initial_cash=initial_cash.get(portfolio_id, default_initial_cash),
        )
        for portfolio_id in PORTFOLIOS
    }
    comparison = []
    for index, trading_date in enumerate(dates):
        rule = portfolios["rule"]["days"][index]
        hermes = portfolios["hermes"]["days"][index]
        comparison.append(
            {
                "date": trading_date.isoformat(),
                "equityDelta": str(hermes["equity"] - rule["equity"]),
                "returnRateDelta": str(
                    hermes["totalReturnRate"] - rule["totalReturnRate"]
                ),
            }
        )
    symbols = [
        {"symbol": symbol, "name": names.get(symbol)}
        for symbol in sorted({str(row[1]) for row in fill_rows})
    ]
    return {
        "meta": {
            "title": "Rule / Hermes Paper Timeline",
            "scope": "paper-only",
            "timezone": "Asia/Seoul",
            "currency": next(iter(currencies), "KRW"),
            "symbols": symbols,
            "dates": [item.isoformat() for item in dates],
            "readOnly": True,
            "generatedAt": datetime.now(UTC).isoformat(),
        },
        "portfolios": portfolios,
        "comparison": comparison,
    }


def _portfolio_days(
    *,
    portfolio_id: str,
    dates: Sequence[date],
    fills: Sequence[dict[str, Any]],
    cycle_rows: Sequence[Sequence[Any]],
    daily_marks: Mapping[date, Mapping[str, dict[str, Any]]],
    latest_marks: dict[str, dict[str, Any]],
    mark_history: Mapping[str, Sequence[dict[str, Any]]],
    names: Mapping[str, str],
    initial_cash: Decimal,
) -> dict[str, Any]:
    portfolio_fills = [item for item in fills if item["portfolioId"] == portfolio_id]
    cumulative: list[dict[str, Any]] = []
    days = []
    cash = initial_cash
    mark_state: dict[str, dict[str, Any]] = dict(latest_marks)
    for trading_date in dates:
        mark_state.update(daily_marks.get(trading_date, {}))
        day_fills = [
            item
            for item in portfolio_fills
            if item["executedAt"].astimezone(SEOUL).date() == trading_date
        ]
        for item in day_fills:
            cumulative.append(item)
            costs = item["commission"] + item["tax"]
            cash += (
                -item["notional"] - costs
                if item["side"] is Side.BUY
                else item["notional"] - costs
            )
        accounting_rows = [
            (
                item["symbol"],
                item["side"].value,
                item["quantity"],
                item["notional"],
                item["commission"],
                item["tax"],
            )
            for item in cumulative
        ]
        accountings = _position_accountings(accounting_rows)
        positions = []
        market_value = Decimal(0)
        unrealized_pnl = Decimal(0)
        for symbol, accounting in sorted(accountings.items()):
            if accounting.quantity == 0:
                continue
            mark = mark_state.get(symbol)
            price = Decimal(mark["price"]) if mark else Decimal(0)
            value = accounting.quantity * price
            unrealized = value - accounting.cost_basis
            market_value += value
            unrealized_pnl += unrealized
            positions.append(
                {
                    "symbol": symbol,
                    "name": mark.get("name") if mark else None,
                    "quantity": str(accounting.quantity),
                    "averageCost": str(accounting.cost_basis / accounting.quantity),
                    "marketPrice": str(price),
                    "marketValue": str(value),
                    "realizedPnl": str(accounting.realized_pnl),
                    "unrealizedPnl": str(unrealized),
                    "totalCosts": str(accounting.total_costs),
                    "markedAt": (
                        mark["markedAt"].isoformat() if mark is not None else None
                    ),
                    "priceTrend": [
                        {
                            "at": point["markedAt"].isoformat(),
                            "price": str(point["price"]),
                        }
                        for point in mark_history.get(symbol, ())
                        if point["markedAt"]
                        <= datetime.combine(trading_date, datetime.max.time(), SEOUL)
                    ][-63:],
                }
            )
        realized_pnl = sum(
            (item.realized_pnl for item in accountings.values()), start=Decimal(0)
        )
        total_costs = sum(
            (item.total_costs for item in accountings.values()), start=Decimal(0)
        )
        equity = cash + market_value
        cycles = [
            row
            for row in cycle_rows
            if str(row[0]) == portfolio_id
            and _datetime(row[3]).astimezone(SEOUL).date() == trading_date
        ]
        days.append(
            {
                "date": trading_date.isoformat(),
                "capturedAt": max(
                    (
                        mark["markedAt"]
                        for mark in mark_state.values()
                        if mark["markedAt"].astimezone(SEOUL).date() <= trading_date
                    ),
                    default=datetime.combine(trading_date, datetime.min.time(), SEOUL),
                ).isoformat(),
                "cash": cash,
                "positionMarketValue": market_value,
                "equity": equity,
                "totalReturnRate": equity / initial_cash - Decimal(1),
                "realizedPnl": realized_pnl,
                "unrealizedPnl": unrealized_pnl,
                "totalCosts": total_costs,
                "positions": positions,
                "trades": [_public_fill(item, names) for item in day_fills],
                "cycles": {
                    "count": len(cycles),
                    "succeeded": sum(str(row[1]) == "succeeded" for row in cycles),
                    "failed": sum(str(row[1]) != "succeeded" for row in cycles),
                    "intervals": sorted({str(row[2]) for row in cycles}),
                },
            }
        )
    return {
        "id": portfolio_id,
        "label": "Rule" if portfolio_id == "rule" else "Hermes",
        "initialCash": str(initial_cash),
        "days": days,
    }


def _fill(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "portfolioId": str(row[0]),
        "symbol": str(row[1]),
        "side": Side(str(row[2])),
        "quantity": Decimal(row[3]),
        "price": Decimal(row[4]),
        "notional": Decimal(row[5]),
        "commission": Decimal(row[6]),
        "tax": Decimal(row[7]),
        "reason": str(row[8]),
        "executedAt": _datetime(row[9]),
    }


def _public_fill(item: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    return {
        "symbol": item["symbol"],
        "name": names.get(str(item["symbol"])),
        "side": item["side"].value,
        "executedAt": item["executedAt"].isoformat(),
        "price": str(item["price"]),
        "quantity": str(item["quantity"]),
        "commission": str(item["commission"]),
        "tax": str(item["tax"]),
        "realizedPnl": "0",
        "reason": item["reason"],
    }


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("paper timeline timestamps must include timezone")
    return parsed
