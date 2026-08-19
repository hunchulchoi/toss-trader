from __future__ import annotations

import json
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
                        SELECT portfolio_id, signal_id, symbol, side, quantity, price,
                               notional, commission, tax, reason, executed_at
                        FROM paper_fills
                        WHERE portfolio_id IN ('rule', 'hermes')
                        ORDER BY executed_at, fill_sequence
                        """
                    )
                    fill_rows = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT portfolio_id, signal_id, symbol, side,
                               signal_reason, approved, violations, evaluated_at
                        FROM paper_risk_decisions
                        WHERE portfolio_id IN ('rule', 'hermes')
                        ORDER BY evaluated_at DESC, decision_id DESC
                        """
                    )
                    risk_rows = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT status, error, details, finished_at
                        FROM automation_run_logs
                        WHERE run_type = 'hermes_trade'
                          AND details->>'portfolioId' = 'hermes'
                        ORDER BY finished_at DESC, run_id DESC
                        """
                    )
                    advice_rows = cursor.fetchall()
                    symbols = sorted({str(row[2]) for row in fill_rows})
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
                            SELECT symbol, open_price, high_price, low_price,
                                   close_price, volume, currency, timestamp
                            FROM market_candles
                            WHERE symbol = ANY(%s) AND interval = '1m'
                              AND timestamp >= %s
                            ORDER BY timestamp, symbol
                            """,
                            (
                                symbols,
                                min(_datetime(row[10]) for row in fill_rows)
                                .astimezone(SEOUL)
                                .replace(hour=0, minute=0, second=0, microsecond=0),
                            ),
                        )
                        minute_rows = cursor.fetchall()
                    else:
                        minute_rows = ()
                    cursor.execute(
                        """
                        SELECT symbol, display_name
                        FROM market_symbols
                        ORDER BY symbol
                        """
                    )
                    name_rows = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT portfolio_id, status, interval, started_at,
                               error_message, run_id, finished_at, symbol_count,
                               signal_count, fill_count, failed_count,
                               consecutive_api_errors, daily_return_rate,
                               cycle_insight
                        FROM paper_cycle_runs
                        WHERE portfolio_id IN ('rule', 'hermes')
                        ORDER BY started_at
                        """
                    )
                    cycle_rows = cursor.fetchall()
                    universe_symbols = _cycle_symbols(cycle_rows)
                    if universe_symbols:
                        cursor.execute(
                            """
                            SELECT symbol, timestamp, close_price
                            FROM (
                                SELECT symbol, timestamp, close_price,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY symbol
                                           ORDER BY timestamp DESC
                                       ) AS recency
                                FROM market_candles
                                WHERE interval = '1d' AND symbol = ANY(%s)
                            ) AS ranked
                            WHERE recency <= 200
                            ORDER BY symbol, timestamp
                            """,
                            (universe_symbols,),
                        )
                        trend_rows = cursor.fetchall()
                    else:
                        trend_rows = ()
                    cursor.execute(
                        """
                        SELECT run_id, run_type, status, stage, started_at,
                               finished_at, prompt_tokens, completion_tokens,
                               total_tokens, error, details
                        FROM automation_run_logs
                        WHERE run_type IN ('hermes_trade', 'market_scan', 'daily')
                        ORDER BY finished_at DESC, run_id DESC
                        LIMIT 200
                        """
                    )
                    hermes_log_rows = cursor.fetchall()
            finally:
                connection.close()
        except self._database_error as error:
            raise RuntimeError("PostgreSQL paper timeline query failed") from error
        return build_paper_timeline(
            initial_rows=initial_rows,
            fill_rows=fill_rows,
            mark_rows=mark_rows,
            cycle_rows=cycle_rows,
            risk_rows=risk_rows,
            advice_rows=advice_rows,
            name_rows=name_rows,
            minute_rows=minute_rows,
            trend_rows=trend_rows,
            hermes_log_rows=hermes_log_rows,
            default_initial_cash=self._initial_cash,
        )


def build_paper_timeline(
    *,
    initial_rows: Sequence[Sequence[Any]],
    fill_rows: Sequence[Sequence[Any]],
    mark_rows: Sequence[Sequence[Any]],
    cycle_rows: Sequence[Sequence[Any]],
    risk_rows: Sequence[Sequence[Any]] = (),
    advice_rows: Sequence[Sequence[Any]] = (),
    name_rows: Sequence[Sequence[Any]] = (),
    minute_rows: Sequence[Sequence[Any]] = (),
    trend_rows: Sequence[Sequence[Any]] = (),
    hermes_log_rows: Sequence[Sequence[Any]] = (),
    default_initial_cash: Decimal,
) -> dict[str, Any]:
    initial_cash = {
        str(portfolio_id): Decimal(value)
        for portfolio_id, value in initial_rows
        if str(portfolio_id) in PORTFOLIOS
    }
    fills = [_fill(row) for row in fill_rows if str(row[0]) in PORTFOLIOS]
    cycle_dates = {
        _datetime(row[3]).astimezone(SEOUL).date()
        for row in cycle_rows
        if str(row[0]) in PORTFOLIOS
    }
    if fills:
        first_date = min(item["executedAt"].astimezone(SEOUL).date() for item in fills)
    elif cycle_dates:
        first_date = min(cycle_dates)
    else:
        first_date = datetime.now(SEOUL).date()
    daily_marks: dict[date, dict[str, dict[str, Any]]] = {}
    mark_history: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    names.update(
        {
            str(symbol): str(display_name)
            for symbol, display_name in name_rows
            if display_name is not None
        }
    )
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
        for symbol in sorted(
            {str(row[2]) for row in fill_rows} | {str(row[2]) for row in risk_rows}
        )
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
        "decisions": _decision_events(
            fills=fills,
            risk_rows=risk_rows,
            advice_rows=advice_rows,
            names=names,
        ),
        "intraday": _intraday_payload(
            minute_rows=minute_rows,
            fills=fills,
            names=names,
        ),
        "cycleTimeline": _cycle_timeline(
            cycle_rows=cycle_rows,
            names=names,
            trend_rows=trend_rows,
        ),
        "hermesConversations": _hermes_conversations(hermes_log_rows, names),
        "errors": _error_events(
            cycle_rows=cycle_rows,
            advice_rows=advice_rows,
            names=names,
        ),
    }


def _cycle_timeline(
    *,
    cycle_rows: Sequence[Sequence[Any]],
    names: Mapping[str, str],
    trend_rows: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    runs = []
    for row in cycle_rows:
        if str(row[0]) not in PORTFOLIOS:
            continue
        started_at = _datetime(row[3])
        finished_at = (
            _datetime(row[6]) if len(row) > 6 and row[6] is not None else None
        )
        insight = _json_mapping(row[13]) if len(row) > 13 else {}
        raw_states = insight.get("symbols")
        symbol_states = []
        if isinstance(raw_states, Sequence) and not isinstance(
            raw_states, (str, bytes)
        ):
            for raw_state in raw_states:
                if not isinstance(raw_state, Mapping):
                    continue
                symbol = str(raw_state.get("symbol") or "")
                symbol_states.append(
                    {
                        "symbol": symbol,
                        "name": names.get(symbol),
                        "reason": raw_state.get("reason"),
                        "skipReason": raw_state.get("skipReason"),
                        "error": raw_state.get("error"),
                        "fillSide": raw_state.get("fillSide"),
                    }
                )
        runs.append(
            {
                "runId": str(row[5]) if len(row) > 5 else None,
                "portfolioId": str(row[0]),
                "status": str(row[1]),
                "interval": str(row[2]),
                "startedAt": started_at.isoformat(),
                "finishedAt": finished_at.isoformat() if finished_at else None,
                "durationMs": (
                    int((finished_at - started_at).total_seconds() * 1000)
                    if finished_at
                    else None
                ),
                "symbolCount": int(row[7]) if len(row) > 7 else 0,
                "signalCount": int(row[8]) if len(row) > 8 else 0,
                "fillCount": int(row[9]) if len(row) > 9 else 0,
                "failedCount": int(row[10]) if len(row) > 10 else 0,
                "consecutiveApiErrors": int(row[11]) if len(row) > 11 else 0,
                "dailyReturnRate": str(row[12]) if len(row) > 12 else "0",
                "error": str(row[4]) if len(row) > 4 and row[4] is not None else None,
                "idleReason": insight.get("idleReason"),
                "newBuysAllowed": insight.get("newBuysAllowed"),
                "funnel": _mapping_or_empty(insight.get("funnel")),
                "reasons": _mapping_or_empty(insight.get("reasons")),
                "symbolStates": symbol_states,
            }
        )
    runs.sort(
        key=lambda item: (str(item["startedAt"]), str(item["portfolioId"])),
        reverse=True,
    )
    trends: dict[str, list[dict[str, str]]] = {}
    for symbol, timestamp, close_price in trend_rows:
        trends.setdefault(str(symbol), []).append(
            {
                "timestamp": _datetime(timestamp).isoformat(),
                "close": str(close_price),
            }
        )
    return {"runs": runs, "trends": trends}


def _cycle_symbols(cycle_rows: Sequence[Sequence[Any]]) -> list[str]:
    symbols: dict[str, None] = {}
    for row in cycle_rows:
        insight = _json_mapping(row[13]) if len(row) > 13 else {}
        raw_states = insight.get("symbols")
        if not isinstance(raw_states, Sequence) or isinstance(raw_states, (str, bytes)):
            continue
        for state in raw_states:
            if not isinstance(state, Mapping):
                continue
            symbol = str(state.get("symbol") or "")
            if symbol:
                symbols[symbol] = None
    return list(symbols)


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
        "signalId": str(row[1]),
        "symbol": str(row[2]),
        "side": Side(str(row[3])),
        "quantity": Decimal(row[4]),
        "price": Decimal(row[5]),
        "notional": Decimal(row[6]),
        "commission": Decimal(row[7]),
        "tax": Decimal(row[8]),
        "reason": str(row[9]),
        "executedAt": _datetime(row[10]),
    }


def _public_fill(item: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    return {
        "portfolioId": item["portfolioId"],
        "symbol": item["symbol"],
        "signalId": item["signalId"],
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


def _decision_events(
    *,
    fills: Sequence[Mapping[str, Any]],
    risk_rows: Sequence[Sequence[Any]],
    advice_rows: Sequence[Sequence[Any]],
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    fills_by_signal = {str(item["signalId"]): item for item in fills}
    advice_by_signal: dict[str, dict[str, Any]] = {}
    for status, error, raw_details, finished_at in advice_rows:
        details = _json_mapping(raw_details)
        signal_id = details.get("signalId")
        if not isinstance(signal_id, str) or signal_id in advice_by_signal:
            continue
        advice_by_signal[signal_id] = {
            "status": str(status),
            "approved": details.get("approved"),
            "rationale": details.get("rationale"),
            "error": str(error) if error is not None else None,
            "finishedAt": _datetime(finished_at).isoformat(),
        }
    events = []
    for row in risk_rows:
        portfolio_id = str(row[0])
        signal_id = str(row[1])
        symbol = str(row[2])
        side = Side(str(row[3]))
        approved = bool(row[5])
        violations = _string_list(row[6])
        fill = fills_by_signal.get(signal_id)
        if fill is not None:
            outcome = "bought" if side is Side.BUY else "sold"
        elif not approved:
            outcome = "rejected"
        else:
            outcome = "approved-not-filled"
        events.append(
            {
                "portfolioId": portfolio_id,
                "signalId": signal_id,
                "symbol": symbol,
                "name": names.get(symbol),
                "side": side.value,
                "outcome": outcome,
                "signalReason": str(row[4]),
                "riskApproved": approved,
                "violations": violations,
                "hermes": advice_by_signal.get(signal_id),
                "evaluatedAt": _datetime(row[7]).isoformat(),
                "executedAt": (
                    fill["executedAt"].isoformat() if fill is not None else None
                ),
            }
        )
    return events


HERMES_KIND_LABELS = {
    "hermes_trade": "종목 판단",
    "market_scan": "장전 분석",
    "daily": "마감 분석",
}


def _hermes_conversations(
    rows: Sequence[Sequence[Any]],
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    conversations = []
    for row in rows:
        details = _json_mapping(row[10])
        run_type = str(row[1])
        symbol = details.get("symbol")
        symbol_text = str(symbol) if symbol is not None else None
        assistant = None
        for key in ("assistant", "rationale", "analysis", "opinion"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                assistant = value.strip()
                break
        conversations.append(
            {
                "runId": str(row[0]),
                "runType": run_type,
                "kind": HERMES_KIND_LABELS.get(run_type, run_type),
                "status": str(row[2]),
                "stage": str(row[3]),
                "startedAt": _datetime(row[4]).isoformat(),
                "finishedAt": _datetime(row[5]).isoformat(),
                "promptTokens": int(row[6] or 0),
                "completionTokens": int(row[7] or 0),
                "totalTokens": int(row[8] or 0),
                "error": None if row[9] is None else str(row[9]),
                "symbol": symbol_text,
                "name": names.get(symbol_text) if symbol_text else None,
                "side": (
                    str(details["side"]) if details.get("side") is not None else None
                ),
                "approved": details.get("approved")
                if isinstance(details.get("approved"), bool)
                else None,
                "assistant": assistant,
                "bodyMissing": assistant is None,
            }
        )
    return conversations


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def _intraday_payload(
    *,
    minute_rows: Sequence[Sequence[Any]],
    fills: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
) -> dict[str, Any]:
    series: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in minute_rows:
        timestamp = _datetime(row[7])
        trading_date = timestamp.astimezone(SEOUL).date().isoformat()
        symbol = str(row[0])
        series.setdefault(trading_date, {}).setdefault(symbol, []).append(
            {
                "at": timestamp.isoformat(),
                "open": str(row[1]),
                "high": str(row[2]),
                "low": str(row[3]),
                "close": str(row[4]),
                "volume": str(row[5]),
            }
        )
    executions: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for fill in fills:
        trading_date = fill["executedAt"].astimezone(SEOUL).date().isoformat()
        executions.setdefault(trading_date, {}).setdefault(
            str(fill["symbol"]), []
        ).append(_public_fill(fill, names))
    symbols = sorted(
        {str(row[0]) for row in minute_rows} | {str(fill["symbol"]) for fill in fills}
    )
    return {
        "symbols": [
            {"symbol": symbol, "name": names.get(symbol)} for symbol in symbols
        ],
        "dates": sorted(series),
        "series": series,
        "executions": executions,
    }


def _error_events(
    *,
    cycle_rows: Sequence[Sequence[Any]],
    advice_rows: Sequence[Sequence[Any]],
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    errors = []
    for row in cycle_rows:
        status = str(row[1])
        message = str(row[4]) if len(row) > 4 and row[4] is not None else None
        if status == "succeeded" and message is None:
            continue
        errors.append(
            {
                "portfolioId": str(row[0]),
                "source": "cycle",
                "status": status,
                "interval": str(row[2]),
                "symbol": None,
                "name": None,
                "message": message or f"cycle status: {status}",
                "occurredAt": _datetime(row[3]).isoformat(),
            }
        )
    for status, error, raw_details, finished_at in advice_rows:
        if str(status) == "succeeded" and error is None:
            continue
        details = _json_mapping(raw_details)
        symbol = details.get("symbol")
        symbol_text = str(symbol) if symbol is not None else None
        errors.append(
            {
                "portfolioId": "hermes",
                "source": "hermes",
                "status": str(status),
                "interval": None,
                "symbol": symbol_text,
                "name": names.get(symbol_text) if symbol_text else None,
                "message": str(error) if error is not None else "Hermes 분석 실패",
                "occurredAt": _datetime(finished_at).isoformat(),
            }
        )
    return sorted(errors, key=lambda item: str(item["occurredAt"]), reverse=True)


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("paper timeline timestamps must include timezone")
    return parsed
