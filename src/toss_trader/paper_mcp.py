from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from .models import Side
from .paper import PositionAccounting, _position_accountings

PORTFOLIOS = ("rule", "hermes")
MAX_REQUEST_BYTES = 1024 * 1024


class PaperReadStore(Protocol):
    def status(self) -> dict[str, Any]: ...

    def holdings(self) -> dict[str, Any]: ...

    def pnl(self) -> dict[str, Any]: ...


class PaperMcpService:
    def __init__(self, store: PaperReadStore) -> None:
        self._store = store

    def tools(self) -> list[dict[str, Any]]:
        empty_schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return [
            {
                "name": "toss_paper_status",
                "description": (
                    "Rule/Hermes paper 자동매매의 마지막 cycle, idleReason, "
                    "종목별 MA 상태, 현금, Hermes 호출, 최근 실패를 읽는다."
                ),
                "inputSchema": empty_schema,
            },
            {
                "name": "toss_paper_holdings",
                "description": (
                    "Rule/Hermes paper 장부의 현재 보유 종목, 수량, 평균원가, "
                    "평가금액을 읽는다."
                ),
                "inputSchema": empty_schema,
            },
            {
                "name": "toss_paper_pnl",
                "description": (
                    "Rule/Hermes paper 장부의 총자산, 실현·미실현손익, "
                    "수수료·세금, 시작현금 대비 손익을 읽는다."
                ),
                "inputSchema": empty_schema,
            },
        ]

    def call(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        if arguments:
            raise ValueError(f"{name} does not accept arguments")
        readers: dict[str, Callable[[], dict[str, Any]]] = {
            "toss_paper_status": self._store.status,
            "toss_paper_holdings": self._store.holdings,
            "toss_paper_pnl": self._store.pnl,
        }
        reader = readers.get(name)
        if reader is None:
            raise ValueError(f"unknown MCP tool: {name}")
        return reader()


class PostgresPaperReadStore:
    """Fixed-query paper reader with PostgreSQL session-level write blocking."""

    def __init__(
        self,
        connection_parameters: Mapping[str, str | int],
        *,
        initial_cash: Decimal = Decimal(1000000),
        connect: Callable[..., Any] | None = None,
    ) -> None:
        required = {"host", "port", "user", "password", "dbname"}
        missing = sorted(required - connection_parameters.keys())
        if missing:
            raise ValueError(f"missing PostgreSQL parameters: {', '.join(missing)}")
        if initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        if connect is None:
            try:
                import psycopg
            except ImportError as error:
                raise RuntimeError(
                    "PostgreSQL support requires: pip install 'toss-trader[postgres]'"
                ) from error
            connect = psycopg.connect
        self._parameters = {
            name: connection_parameters[name] for name in required
        }
        self._initial_cash = initial_cash
        self._connect = connect

    def status(self) -> dict[str, Any]:
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT portfolio_id, run_id, started_at, finished_at, status,
                           interval, symbol_count, signal_count, fill_count,
                           failed_count, consecutive_api_errors, daily_return_rate,
                           error_message, cycle_insight
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY portfolio_id
                            ORDER BY started_at DESC, run_id DESC
                        ) AS row_number
                        FROM paper_cycle_runs
                        WHERE portfolio_id IN ('rule', 'hermes')
                    ) AS ranked
                    WHERE row_number = 1
                    ORDER BY portfolio_id
                    """
                )
                cycle_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT run_id, run_type, status, stage, started_at, finished_at,
                           duration_ms, prompt_tokens, completion_tokens,
                           total_tokens, error
                    FROM automation_run_logs
                    WHERE run_type = 'hermes_trade' OR stage = 'hermes-analysis'
                    ORDER BY finished_at DESC, run_id DESC
                    LIMIT 1
                    """
                )
                hermes_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT portfolio_id, status, started_at, finished_at, error_message
                    FROM paper_cycle_runs
                    WHERE portfolio_id IN ('rule', 'hermes')
                      AND status IN ('partial_failure', 'failed')
                    ORDER BY started_at DESC, run_id DESC
                    LIMIT 10
                    """
                )
                cycle_failures = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT run_type, status, stage, started_at, finished_at, error
                    FROM automation_run_logs
                    WHERE status = 'failed'
                    ORDER BY finished_at DESC, run_id DESC
                    LIMIT 10
                    """
                )
                automation_failures = cursor.fetchall()
        finally:
            connection.close()
        cycles = {str(row[0]): _cycle_status(row) for row in cycle_rows}
        for portfolio_id in PORTFOLIOS:
            cycles.setdefault(portfolio_id, None)
        ledgers = self._portfolio_states()
        for portfolio_id, cycle in cycles.items():
            if cycle is None:
                continue
            cycle.update(_ledger_status(ledgers[portfolio_id]))
        failures = [
            {
                "source": "cycle",
                "portfolioId": str(row[0]),
                "status": str(row[1]),
                "startedAt": _iso(row[2]),
                "finishedAt": _iso(row[3]),
                "error": row[4],
            }
            for row in cycle_failures
        ] + [
            {
                "source": "automation",
                "runType": str(row[0]),
                "status": str(row[1]),
                "stage": str(row[2]),
                "startedAt": _iso(row[3]),
                "finishedAt": _iso(row[4]),
                "error": row[5],
            }
            for row in automation_failures
        ]
        failures.sort(
            key=lambda item: str(item.get("finishedAt") or item.get("startedAt") or ""),
            reverse=True,
        )
        return {
            "generatedAt": datetime.now(UTC).isoformat(),
            "scope": "paper-only",
            "cycles": cycles,
            "latestHermesCall": _hermes_status(hermes_row) if hermes_row else None,
            "recentFailures": failures[:10],
        }

    def holdings(self) -> dict[str, Any]:
        states = self._portfolio_states()
        return {
            "generatedAt": datetime.now(UTC).isoformat(),
            "scope": "paper-only",
            "portfolios": {
                portfolio_id: [
                    _holding(symbol, accounting, state["marks"].get(symbol))
                    for symbol, accounting in sorted(state["accountings"].items())
                    if accounting.quantity > 0
                ]
                for portfolio_id, state in states.items()
            },
        }

    def pnl(self) -> dict[str, Any]:
        states = self._portfolio_states()
        return {
            "generatedAt": datetime.now(UTC).isoformat(),
            "scope": "paper-only",
            "portfolios": {
                portfolio_id: _pnl(state) for portfolio_id, state in states.items()
            },
        }

    def _open(self) -> Any:
        return self._connect(
            **self._parameters,
            application_name="toss-paper-mcp",
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=5000 "
                "-c idle_in_transaction_session_timeout=5000"
            ),
        )

    def _portfolio_states(self) -> dict[str, dict[str, Any]]:
        connection = self._open()
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
                    SELECT portfolio_id, symbol, side, quantity, notional,
                           commission, tax
                    FROM paper_fills
                    WHERE portfolio_id IN ('rule', 'hermes')
                    ORDER BY portfolio_id, executed_at, fill_sequence
                    """
                )
                fill_rows = cursor.fetchall()
                symbols = sorted({str(row[1]) for row in fill_rows})
                mark_rows: Sequence[Sequence[Any]] = ()
                if symbols:
                    cursor.execute(
                        """
                        SELECT DISTINCT ON (c.symbol)
                               c.symbol, s.display_name, c.close_price,
                               c.currency, c.timestamp
                        FROM market_candles AS c
                        LEFT JOIN market_symbols AS s ON s.symbol = c.symbol
                        WHERE c.symbol = ANY(%s)
                        ORDER BY c.symbol, c.timestamp DESC,
                                 CASE c.interval WHEN '1m' THEN 0 ELSE 1 END
                        """,
                        (symbols,),
                    )
                    mark_rows = cursor.fetchall()
        finally:
            connection.close()
        initial_cash = {
            str(portfolio_id): Decimal(value)
            for portfolio_id, value in initial_rows
        }
        grouped: dict[str, list[Sequence[Any]]] = {key: [] for key in PORTFOLIOS}
        cash_flows = {key: Decimal(0) for key in PORTFOLIOS}
        for row in fill_rows:
            portfolio_id = str(row[0])
            if portfolio_id not in grouped:
                continue
            grouped[portfolio_id].append(row[1:])
            side = Side(str(row[2]))
            notional = Decimal(row[4])
            costs = Decimal(row[5]) + Decimal(row[6])
            cash_flows[portfolio_id] += (
                -notional - costs if side is Side.BUY else notional - costs
            )
        marks = {
            str(row[0]): {
                "name": str(row[1]) if row[1] is not None else None,
                "price": Decimal(row[2]),
                "currency": str(row[3]).upper(),
                "markedAt": _iso(row[4]),
            }
            for row in mark_rows
        }
        return {
            portfolio_id: {
                "initialCash": initial_cash.get(portfolio_id, self._initial_cash),
                "cash": initial_cash.get(portfolio_id, self._initial_cash)
                + cash_flows[portfolio_id],
                "accountings": _position_accountings(grouped[portfolio_id]),
                "marks": marks,
            }
            for portfolio_id in PORTFOLIOS
        }


def handle_mcp_request(
    service: PaperMcpService, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    request_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str):
        return _rpc_error(request_id, -32600, "invalid request")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = payload.get("params")
        params = params if isinstance(params, Mapping) else {}
        requested_version = params.get("protocolVersion")
        return _rpc_result(
            request_id,
            {
                "protocolVersion": (
                    requested_version
                    if isinstance(requested_version, str)
                    else "2025-06-18"
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "toss-paper-readonly", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": service.tools()})
    if method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, Mapping) or not isinstance(params.get("name"), str):
            return _rpc_error(request_id, -32602, "invalid tool call parameters")
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, Mapping):
            return _rpc_error(request_id, -32602, "tool arguments must be an object")
        try:
            result = service.call(str(params["name"]), arguments)
        except (RuntimeError, ValueError) as error:
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            )
        text = json.dumps(result, ensure_ascii=False, default=_json_default)
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": result,
                "isError": False,
            },
        )
    return _rpc_error(request_id, -32601, f"method not found: {method}")


def serve_paper_mcp(
    *, host: str, port: int, service: PaperMcpService
) -> None:
    if not host.strip():
        raise ValueError("MCP host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("MCP port must be between 1 and 65535")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok", "tools": 3})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"error": "invalid content length"})
                return
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._send_json(413, {"error": "request body size is invalid"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, _rpc_error(None, -32700, "parse error"))
                return
            if not isinstance(payload, Mapping):
                self._send_json(400, _rpc_error(None, -32600, "invalid request"))
                return
            response = handle_mcp_request(service, payload)
            if response is None:
                self.send_response(202)
                self.end_headers()
                return
            self._send_json(200, response)

        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, default=_json_default
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _cycle_status(row: Sequence[Any]) -> dict[str, Any]:
    payload = {
        "runId": str(row[1]),
        "startedAt": _iso(row[2]),
        "finishedAt": _iso(row[3]),
        "status": str(row[4]),
        "interval": str(row[5]),
        "symbols": int(row[6]),
        "signals": int(row[7]),
        "fills": int(row[8]),
        "failed": int(row[9]),
        "consecutiveApiErrors": int(row[10]),
        "dailyReturnRate": str(row[11]),
        "error": row[12],
        "idleReason": None,
        "newBuysAllowed": None,
        "funnel": None,
        "reasons": None,
        "symbolStates": [],
    }
    insight = _parse_cycle_insight(row[13] if len(row) > 13 else None)
    if insight is None:
        return payload
    payload["idleReason"] = insight.get("idleReason")
    payload["newBuysAllowed"] = insight.get("newBuysAllowed")
    payload["funnel"] = insight.get("funnel")
    payload["reasons"] = insight.get("reasons")
    payload["symbolStates"] = insight.get("symbols") or []
    return payload


def _parse_cycle_insight(raw: Any) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _ledger_status(state: Mapping[str, Any]) -> dict[str, Any]:
    pnl = _pnl(state)
    open_position_count = sum(
        1
        for accounting in state["accountings"].values()
        if accounting.quantity > 0
    )
    cash = Decimal(pnl["cash"])
    equity = pnl["equity"]
    cash_weight = (
        str(cash / Decimal(equity)) if equity not in (None, "") else None
    )
    return {
        "cash": pnl["cash"],
        "cashWeight": cash_weight,
        "openPositionCount": open_position_count,
    }


def _hermes_status(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "runId": str(row[0]),
        "runType": str(row[1]),
        "status": str(row[2]),
        "stage": str(row[3]),
        "startedAt": _iso(row[4]),
        "finishedAt": _iso(row[5]),
        "durationMs": int(row[6]),
        "promptTokens": int(row[7]),
        "completionTokens": int(row[8]),
        "totalTokens": int(row[9]),
        "error": row[10],
    }


def _holding(
    symbol: str,
    accounting: PositionAccounting,
    mark: Mapping[str, Any] | None,
) -> dict[str, Any]:
    market_value = accounting.quantity * mark["price"] if mark else None
    return {
        "symbol": symbol,
        "name": mark.get("name") if mark else None,
        "quantity": str(accounting.quantity),
        "averageCost": str(accounting.cost_basis / accounting.quantity),
        "costBasis": str(accounting.cost_basis),
        "markPrice": str(mark["price"]) if mark else None,
        "marketValue": str(market_value) if market_value is not None else None,
        "unrealizedPnl": (
            str(market_value - accounting.cost_basis)
            if market_value is not None
            else None
        ),
        "currency": mark.get("currency") if mark else None,
        "markedAt": mark.get("markedAt") if mark else None,
    }


def _pnl(state: Mapping[str, Any]) -> dict[str, Any]:
    accountings: Mapping[str, PositionAccounting] = state["accountings"]
    marks: Mapping[str, Mapping[str, Any]] = state["marks"]
    open_positions = {
        symbol: accounting
        for symbol, accounting in accountings.items()
        if accounting.quantity > 0
    }
    missing_marks = sorted(set(open_positions) - marks.keys())
    currencies = sorted(
        {str(marks[symbol]["currency"]) for symbol in open_positions if symbol in marks}
    )
    complete = not missing_marks and len(currencies) <= 1
    market_value = sum(
        (
            accounting.quantity * marks[symbol]["price"]
            for symbol, accounting in open_positions.items()
            if symbol in marks
        ),
        Decimal(0),
    )
    cost_basis = sum(
        (accounting.cost_basis for accounting in open_positions.values()), Decimal(0)
    )
    realized = sum(
        (accounting.realized_pnl for accounting in accountings.values()), Decimal(0)
    )
    commission = sum(
        (accounting.commission for accounting in accountings.values()), Decimal(0)
    )
    tax = sum((accounting.tax for accounting in accountings.values()), Decimal(0))
    initial_cash = Decimal(state["initialCash"])
    cash = Decimal(state["cash"])
    equity = cash + market_value if complete else None
    return {
        "valuationComplete": complete,
        "missingMarks": missing_marks,
        "currency": currencies[0] if len(currencies) == 1 else None,
        "startingCash": str(initial_cash),
        "cash": str(cash),
        "costBasis": str(cost_basis),
        "marketValue": str(market_value) if complete else None,
        "equity": str(equity) if equity is not None else None,
        "pnlFromStartingCash": str(equity - initial_cash) if equity is not None else None,
        "returnRate": str(equity / initial_cash - 1) if equity is not None else None,
        "realizedPnl": str(realized),
        "unrealizedPnl": str(market_value - cost_basis) if complete else None,
        "commission": str(commission),
        "tax": str(tax),
        "totalCosts": str(commission + tax),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _rpc_result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: Any, code: int, message: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
