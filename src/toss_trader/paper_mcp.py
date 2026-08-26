from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from .models import Side
from .paper import PositionAccounting, _position_accountings

PORTFOLIOS = ("rule", "hermes")
MAX_REQUEST_BYTES = 1024 * 1024
SEOUL = ZoneInfo("Asia/Seoul")
PANEL_EVIDENCE_TOPICS = ("session-summary", "symbol-trace")
PANEL_EVIDENCE_SYMBOL_LIMIT = 10
PANEL_EVIDENCE_MAX_AGE = timedelta(days=31)
SYMBOL_PATTERN = re.compile(r"^[0-9A-Z.]{1,20}$")
PUBLIC_MCP_TOOLS = (
    "toss_paper_status",
    "toss_paper_holdings",
    "toss_paper_pnl",
)
PANEL_MCP_TOOLS = ("toss_paper_panel_evidence",)


class PaperReadStore(Protocol):
    def status(self) -> dict[str, Any]: ...

    def holdings(self) -> dict[str, Any]: ...

    def pnl(self) -> dict[str, Any]: ...

    def panel_evidence(
        self, panel_id: str, topic: str, symbols: tuple[str, ...]
    ) -> dict[str, Any]: ...


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
            {
                "name": "toss_paper_panel_evidence",
                "description": (
                    "중간·마감 패널 JSON에 체결 시각·가격, 전체 종목 전이, "
                    "거절 상세가 부족할 때 해당 panel의 관측시각까지만 paper 원장을 "
                    "고정 SELECT로 검색한다. session-summary는 cycle·체결·현금·손익 "
                    "요약, symbol-trace는 지정 종목의 사유 전이·Risk·D-1/1분봉 "
                    "근거를 반환한다. 임의 SQL, 주문, panel 관측 이후 데이터는 허용하지 "
                    "않는다. symbol-trace에는 symbols 1~10개가 필요하다."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "panelId": {
                            "type": "string",
                            "format": "uuid",
                            "description": (
                                "현재 분석 중인 daily panel UUID. prompt의 PANEL_ID를 "
                                "그대로 사용하고 재구성하지 않는다."
                            ),
                        },
                        "topic": {
                            "type": "string",
                            "enum": list(PANEL_EVIDENCE_TOPICS),
                            "description": (
                                "전체 장부 요약은 session-summary, 특정 종목 원인 검증은 "
                                "symbol-trace."
                            ),
                        },
                        "symbols": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "pattern": SYMBOL_PATTERN.pattern,
                            },
                            "maxItems": PANEL_EVIDENCE_SYMBOL_LIMIT,
                            "description": (
                                "symbol-trace에서 확인할 정확한 종목코드 배열. "
                                "session-summary에서는 생략한다."
                            ),
                        },
                    },
                    "required": ["panelId", "topic"],
                    "additionalProperties": False,
                },
            },
        ]

    def call(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        readers: dict[str, Callable[[], dict[str, Any]]] = {
            "toss_paper_status": self._store.status,
            "toss_paper_holdings": self._store.holdings,
            "toss_paper_pnl": self._store.pnl,
        }
        reader = readers.get(name)
        if reader is not None:
            if arguments:
                raise ValueError(f"{name} does not accept arguments")
            return reader()
        if name != "toss_paper_panel_evidence":
            raise ValueError(f"unknown MCP tool: {name}")
        panel_id, topic, symbols = _panel_evidence_arguments(arguments)
        return self._store.panel_evidence(panel_id, topic, symbols)


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

    def panel_evidence(
        self, panel_id: str, topic: str, symbols: tuple[str, ...]
    ) -> dict[str, Any]:
        connection = self._open()
        try:
            panel = _panel_cutoff(connection, panel_id)
            if topic == "session-summary":
                return self._panel_session_summary(connection, panel_id, panel)
            return self._panel_symbol_trace(
                connection, panel_id, panel, symbols=symbols
            )
        finally:
            connection.close()

    def _panel_session_summary(
        self,
        connection: Any,
        panel_id: str,
        panel: Mapping[str, Any],
    ) -> dict[str, Any]:
        session_start = panel["sessionStart"]
        market_open = panel["marketOpen"]
        observed_at = panel["observedAt"]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT portfolio_id, status, interval, signal_count, fill_count,
                       failed_count, consecutive_api_errors, started_at
                FROM paper_cycle_runs
                WHERE portfolio_id IN ('rule', 'hermes')
                  AND started_at >= %s AND started_at <= %s
                ORDER BY portfolio_id, started_at
                """,
                (session_start, observed_at),
            )
            cycle_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT f.portfolio_id, f.symbol,
                       COALESCE(names.display_name, f.symbol), f.side,
                       f.quantity, f.price, f.notional, f.commission, f.tax,
                       f.reason, f.executed_at
                FROM paper_fills AS f
                LEFT JOIN LATERAL (
                    SELECT display_name
                    FROM market_universe_raw_v2
                    WHERE symbol = f.symbol AND session_date <= %s
                    ORDER BY session_date DESC, source
                    LIMIT 1
                ) AS names ON TRUE
                WHERE f.portfolio_id IN ('rule', 'hermes')
                  AND f.executed_at >= %s AND f.executed_at <= %s
                ORDER BY f.executed_at, f.fill_sequence
                """,
                (panel["businessDate"], market_open, observed_at),
            )
            fill_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT DISTINCT ON (portfolio_id)
                       portfolio_id, captured_at, equity, realized_pnl,
                       unrealized_pnl, total_costs
                FROM paper_portfolio_snapshots
                WHERE portfolio_id IN ('rule', 'hermes')
                  AND captured_at >= %s AND captured_at <= %s
                ORDER BY portfolio_id, captured_at DESC
                """,
                (market_open, observed_at),
            )
            snapshot_rows = cursor.fetchall()
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
                SELECT portfolio_id,
                       COALESCE(SUM(
                           CASE side
                               WHEN 'BUY' THEN -(notional + commission + tax)
                               ELSE notional - commission - tax
                           END
                       ), 0)
                FROM paper_fills
                WHERE portfolio_id IN ('rule', 'hermes')
                  AND executed_at <= %s
                GROUP BY portfolio_id
                """,
                (observed_at,),
            )
            cash_flow_rows = cursor.fetchall()
        initial_cash = {
            str(portfolio): Decimal(value) for portfolio, value in initial_rows
        }
        cash_flows = {
            str(portfolio): Decimal(value) for portfolio, value in cash_flow_rows
        }
        cycles = {}
        for portfolio in PORTFOLIOS:
            rows = [row for row in cycle_rows if str(row[0]) == portfolio]
            pre_market_count = sum(row[7] < market_open for row in rows)
            cycles[portfolio] = {
                "count": len(rows),
                "preMarketCount": pre_market_count,
                "fromMarketOpenCount": len(rows) - pre_market_count,
                "firstAt": _iso(rows[0][7]) if rows else None,
                "lastAt": _iso(rows[-1][7]) if rows else None,
                "signals": sum(int(row[3]) for row in rows),
                "fills": sum(int(row[4]) for row in rows),
                "failedItems": sum(int(row[5]) for row in rows),
                "failedCycles": sum(str(row[1]) == "failed" for row in rows),
                "maxConsecutiveApiErrors": max(
                    (int(row[6]) for row in rows), default=0
                ),
            }
        snapshots = {
            str(row[0]): {
                "capturedAt": _iso(row[1]),
                "equity": str(row[2]),
                "realizedPnl": str(row[3]),
                "unrealizedPnl": str(row[4]),
                "totalCosts": str(row[5]),
            }
            for row in snapshot_rows
        }
        fills = [
            {
                "portfolioId": str(row[0]),
                "symbol": str(row[1]),
                "name": str(row[2]),
                "side": str(row[3]),
                "quantity": str(row[4]),
                "price": str(row[5]),
                "notional": str(row[6]),
                "commission": str(row[7]),
                "tax": str(row[8]),
                "reason": str(row[9]),
                "executedAt": _iso(row[10]),
            }
            for row in fill_rows
        ]
        return _panel_evidence_envelope(
            panel_id,
            panel,
            topic="session-summary",
            evidence={
                "cycles": cycles,
                "fills": fills,
                "snapshots": snapshots,
                "ledgerCashAtCutoff": {
                    portfolio: str(
                        initial_cash.get(portfolio, self._initial_cash)
                        + cash_flows.get(portfolio, Decimal(0))
                    )
                    for portfolio in PORTFOLIOS
                },
            },
        )

    def _panel_symbol_trace(
        self,
        connection: Any,
        panel_id: str,
        panel: Mapping[str, Any],
        *,
        symbols: tuple[str, ...],
    ) -> dict[str, Any]:
        session_start = panel["sessionStart"]
        market_open = panel["marketOpen"]
        observed_at = panel["observedAt"]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT portfolio_id, started_at, cycle_insight
                FROM paper_cycle_runs
                WHERE portfolio_id IN ('rule', 'hermes')
                  AND started_at >= %s AND started_at <= %s
                ORDER BY portfolio_id, started_at
                """,
                (market_open, observed_at),
            )
            cycle_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT portfolio_id, symbol, side, quantity, price, notional,
                       commission, tax, reason, executed_at
                FROM paper_fills
                WHERE portfolio_id IN ('rule', 'hermes')
                  AND symbol = ANY(%s)
                  AND executed_at >= %s AND executed_at <= %s
                ORDER BY executed_at, fill_sequence
                """,
                (list(symbols), market_open, observed_at),
            )
            fill_rows = cursor.fetchall()
            cursor.execute(
                """
                WITH ranked AS (
                    SELECT portfolio_id, symbol, side, quantity,
                           reference_price, notional, approved, violations,
                           available_cash, daily_buy_count, daily_return_rate,
                           consecutive_api_errors, evaluated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY portfolio_id, symbol
                               ORDER BY evaluated_at
                           ) AS first_rank,
                           ROW_NUMBER() OVER (
                               PARTITION BY portfolio_id, symbol
                               ORDER BY evaluated_at DESC
                           ) AS last_rank
                    FROM paper_risk_decisions
                    WHERE portfolio_id IN ('rule', 'hermes')
                      AND symbol = ANY(%s)
                      AND evaluated_at >= %s AND evaluated_at <= %s
                )
                SELECT portfolio_id, symbol, side, quantity, reference_price,
                       notional, approved, violations, available_cash,
                       daily_buy_count, daily_return_rate,
                       consecutive_api_errors, evaluated_at
                FROM ranked
                WHERE first_rank = 1 OR last_rank = 1
                ORDER BY evaluated_at
                """,
                (list(symbols), market_open, observed_at),
            )
            risk_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT DISTINCT ON (symbol)
                       symbol, display_name
                FROM market_universe_raw_v2
                WHERE symbol = ANY(%s) AND session_date <= %s
                ORDER BY symbol, session_date DESC, source
                """,
                (list(symbols), panel["businessDate"]),
            )
            names = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT DISTINCT ON (symbol)
                       symbol, timestamp, open_price, high_price,
                       low_price, close_price
                FROM market_candles
                WHERE symbol = ANY(%s) AND interval = '1d'
                  AND timestamp < %s
                ORDER BY symbol, timestamp DESC
                """,
                (list(symbols), session_start),
            )
            daily_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT symbol, timestamp, open_price, high_price,
                       low_price, close_price
                FROM market_candles
                WHERE symbol = ANY(%s) AND interval = '1m'
                  AND timestamp > %s AND timestamp <= %s
                ORDER BY symbol, timestamp
                """,
                (list(symbols), panel["marketOpen"], observed_at),
            )
            minute_rows = cursor.fetchall()
        traces = _symbol_reason_traces(cycle_rows, symbols)
        daily = {
            str(row[0]): {
                "timestamp": _iso(row[1]),
                "open": str(row[2]),
                "high": str(row[3]),
                "low": str(row[4]),
                "close": str(row[5]),
                "revisionBoundary": "current-stored-adjusted-candle",
            }
            for row in daily_rows
        }
        minute = _minute_evidence(minute_rows, symbols)
        fills = [
            {
                "portfolioId": str(row[0]),
                "symbol": str(row[1]),
                "side": str(row[2]),
                "quantity": str(row[3]),
                "price": str(row[4]),
                "notional": str(row[5]),
                "commission": str(row[6]),
                "tax": str(row[7]),
                "reason": str(row[8]),
                "executedAt": _iso(row[9]),
            }
            for row in fill_rows
        ]
        risks = [
            {
                "portfolioId": str(row[0]),
                "symbol": str(row[1]),
                "side": str(row[2]),
                "quantity": str(row[3]),
                "referencePrice": str(row[4]),
                "notional": str(row[5]),
                "approved": bool(row[6]),
                "violations": row[7],
                "availableCash": str(row[8]) if row[8] is not None else None,
                "dailyBuyCount": int(row[9]),
                "dailyReturnRate": str(row[10]),
                "consecutiveApiErrors": int(row[11]),
                "evaluatedAt": _iso(row[12]),
            }
            for row in risk_rows
        ]
        return _panel_evidence_envelope(
            panel_id,
            panel,
            topic="symbol-trace",
            evidence={
                "symbols": [
                    {
                        "symbol": symbol,
                        "name": names.get(symbol),
                        "reasonTraces": traces.get(symbol, {}),
                        "previousDaily": daily.get(symbol),
                        "oneMinute": minute.get(symbol),
                    }
                    for symbol in symbols
                ],
                "fills": fills,
                "riskDecisions": risks,
            },
        )

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


def _panel_evidence_arguments(
    arguments: Mapping[str, Any] | None,
) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(arguments, Mapping):
        raise TypeError("toss_paper_panel_evidence arguments must be an object")
    unknown = sorted(set(arguments) - {"panelId", "topic", "symbols"})
    if unknown:
        raise ValueError(
            "toss_paper_panel_evidence unknown arguments: " + ", ".join(unknown)
        )
    try:
        panel_id = str(UUID(str(arguments["panelId"])))
    except (KeyError, ValueError) as error:
        raise ValueError("panelId must be a UUID") from error
    topic = arguments.get("topic")
    if topic not in PANEL_EVIDENCE_TOPICS:
        raise ValueError(
            "topic must be session-summary or symbol-trace"
        )
    raw_symbols = arguments.get("symbols", ())
    if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, (str, bytes)):
        raise TypeError("symbols must be an array")
    if len(raw_symbols) > PANEL_EVIDENCE_SYMBOL_LIMIT:
        raise ValueError(
            f"symbols supports at most {PANEL_EVIDENCE_SYMBOL_LIMIT} values"
        )
    if any(not isinstance(value, str) for value in raw_symbols):
        raise TypeError("symbols values must be strings")
    symbols = tuple(dict.fromkeys(value.strip().upper() for value in raw_symbols))
    if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ValueError("symbols contains an invalid symbol")
    if topic == "session-summary" and symbols:
        raise ValueError("session-summary does not accept symbols")
    if topic == "symbol-trace" and not symbols:
        raise ValueError("symbol-trace requires symbols")
    return panel_id, str(topic), symbols


def _panel_cutoff(connection: Any, panel_id: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT context
            FROM daily_analysis_panels
            WHERE panel_id = %s
            """,
            (panel_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError("daily panel not found")
    context = row[0]
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except json.JSONDecodeError as error:
            raise ValueError("daily panel context is invalid") from error
    if not isinstance(context, Mapping):
        raise TypeError("daily panel context is invalid")
    briefing = context.get("briefing")
    if not isinstance(briefing, Mapping):
        raise TypeError("daily panel briefing is missing")
    raw_observed_at = briefing.get("observedAt")
    if not isinstance(raw_observed_at, str):
        raise TypeError("daily panel observedAt is missing")
    try:
        observed_at = datetime.fromisoformat(raw_observed_at)
    except ValueError as error:
        raise ValueError("daily panel observedAt is invalid") from error
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("daily panel observedAt must include timezone")
    now = datetime.now(UTC)
    observed_utc = observed_at.astimezone(UTC)
    if observed_utc > now + timedelta(minutes=5):
        raise ValueError("daily panel observedAt is in the future")
    if now - observed_utc > PANEL_EVIDENCE_MAX_AGE:
        raise ValueError("daily panel is older than the evidence search window")
    local = observed_at.astimezone(SEOUL)
    business_date = local.date()
    session_start = datetime.combine(business_date, datetime.min.time(), tzinfo=SEOUL)
    market_open = session_start.replace(hour=9)
    return {
        "businessDate": business_date,
        "briefingKind": str(briefing.get("kind") or "close"),
        "observedAt": observed_at,
        "sessionStart": session_start,
        "marketOpen": market_open,
    }


def _panel_evidence_envelope(
    panel_id: str,
    panel: Mapping[str, Any],
    *,
    topic: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "scope": "paper-panel-readonly",
        "panelId": panel_id,
        "topic": topic,
        "businessDate": panel["businessDate"].isoformat(),
        "briefingKind": panel["briefingKind"],
        "observedAt": panel["observedAt"].isoformat(),
        "cutoffEnforced": True,
        "writesAllowed": False,
        "evidence": evidence,
    }


def _symbol_reason_traces(
    cycle_rows: Sequence[Sequence[Any]], symbols: Sequence[str]
) -> dict[str, dict[str, Any]]:
    wanted = set(symbols)
    observations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for portfolio_id, started_at, raw_insight in cycle_rows:
        insight = _parse_cycle_insight(raw_insight)
        if insight is None:
            continue
        states = insight.get("symbols")
        if not isinstance(states, Sequence) or isinstance(states, (str, bytes)):
            continue
        for state in states:
            if not isinstance(state, Mapping):
                continue
            symbol = str(state.get("symbol") or "")
            if symbol not in wanted:
                continue
            reason = state.get("skipReason")
            if not isinstance(reason, str) or not reason:
                fill_side = state.get("fillSide")
                reason = (
                    f"filled:{fill_side}"
                    if isinstance(fill_side, str) and fill_side
                    else str(state.get("reason") or "unknown")
                )
            observations.setdefault((symbol, str(portfolio_id)), []).append(
                {
                    "at": _iso(started_at),
                    "reason": reason,
                    "error": state.get("error"),
                    "detail": state.get("skipDetail"),
                }
            )
    result: dict[str, dict[str, Any]] = {symbol: {} for symbol in symbols}
    for (symbol, portfolio), rows in observations.items():
        path = []
        counts: dict[str, int] = {}
        transition_rows = []
        previous = None
        for row in rows:
            reason = str(row["reason"])
            counts[reason] = counts.get(reason, 0) + 1
            if reason != previous:
                path.append(reason)
                transition_rows.append(row)
                previous = reason
        result[symbol][portfolio] = {
            "firstObservedAt": rows[0]["at"],
            "lastObservedAt": rows[-1]["at"],
            "firstReason": rows[0]["reason"],
            "lastReason": rows[-1]["reason"],
            "transitionCount": max(0, len(path) - 1),
            "reasonPath": path,
            "reasonCounts": counts,
            "transitions": transition_rows,
        }
    return result


def _minute_evidence(
    minute_rows: Sequence[Sequence[Any]], symbols: Sequence[str]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Sequence[Any]]] = {symbol: [] for symbol in symbols}
    for row in minute_rows:
        symbol = str(row[0])
        if symbol in grouped:
            grouped[symbol].append(row)
    result = {}
    for symbol, rows in grouped.items():
        if not rows:
            result[symbol] = {"barCount": 0, "coverage": "missing-1m"}
            continue
        first = rows[0]
        last = rows[-1]
        key_bars = [
            _minute_row(row)
            for row in rows
            if row[1].astimezone(SEOUL).time().isoformat(timespec="minutes")
            in {"09:01", "09:05"}
        ]
        result[symbol] = {
            "barCount": len(rows),
            "firstAt": _iso(first[1]),
            "lastAt": _iso(last[1]),
            "firstBar": _minute_row(first),
            "keyBars": key_bars,
            "lastBar": _minute_row(last),
            "coverage": "observed-through-panel-cutoff",
        }
    return result


def _minute_row(row: Sequence[Any]) -> dict[str, str | None]:
    return {
        "timestamp": _iso(row[1]),
        "open": str(row[2]),
        "high": str(row[3]),
        "low": str(row[4]),
        "close": str(row[5]),
    }


def handle_mcp_request(
    service: PaperMcpService,
    payload: Mapping[str, Any],
    *,
    allowed_tools: Sequence[str] | None = None,
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
        tools = service.tools()
        if allowed_tools is not None:
            allowed = set(allowed_tools)
            tools = [tool for tool in tools if tool["name"] in allowed]
        return _rpc_result(request_id, {"tools": tools})
    if method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, Mapping) or not isinstance(params.get("name"), str):
            return _rpc_error(request_id, -32602, "invalid tool call parameters")
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, Mapping):
            return _rpc_error(request_id, -32602, "tool arguments must be an object")
        try:
            name = str(params["name"])
            if allowed_tools is not None and name not in allowed_tools:
                raise ValueError(f"MCP endpoint does not expose tool: {name}")
            result = service.call(name, arguments)
        except (RuntimeError, TypeError, ValueError) as error:
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
                self._send_json(200, {"status": "ok", "tools": 4})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            endpoint_tools = {
                "/mcp": PUBLIC_MCP_TOOLS,
                "/panel-mcp": PANEL_MCP_TOOLS,
            }
            allowed_tools = endpoint_tools.get(self.path)
            if allowed_tools is None:
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
            response = handle_mcp_request(
                service, payload, allowed_tools=allowed_tools
            )
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
