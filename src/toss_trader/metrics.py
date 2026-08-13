from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .cycle_state import PaperCycleRun

RUN_STATUSES = ("running", "succeeded", "partial_failure", "failed")
COMPLETED_RUN_STATUSES = ("succeeded", "partial_failure", "failed")

LATEST_RUN_SQL = """
SELECT run_id, started_at, finished_at, status, interval,
       symbol_count, signal_count, fill_count, failed_count,
       consecutive_api_errors, daily_return_rate, error_message
FROM paper_cycle_runs
WHERE status <> 'running'
ORDER BY started_at DESC, run_id DESC
LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    generated_at: datetime
    latest_run: PaperCycleRun | None
    run_counts: dict[str, int]
    paper_fill_count: int
    position_quantities: dict[str, Decimal]
    paper_cash_change: Decimal = Decimal(0)


class MetricsStore(Protocol):
    def close(self) -> None: ...

    def snapshot(self, *, generated_at: datetime) -> MetricsSnapshot: ...


class SqliteMetricsStore:
    def __init__(self, database_path: str) -> None:
        if database_path == ":memory:":
            raise ValueError("metrics requires a file-backed SQLite database")
        uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
        try:
            self._connection = sqlite3.connect(uri, uri=True)
            self._connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error as error:
            raise RuntimeError("SQLite metrics connection failed") from error

    def close(self) -> None:
        self._connection.close()

    def snapshot(self, *, generated_at: datetime) -> MetricsSnapshot:
        _require_aware(generated_at)
        try:
            latest_row = self._connection.execute(LATEST_RUN_SQL).fetchone()
            count_rows = self._connection.execute(
                "SELECT status, COUNT(*) FROM paper_cycle_runs GROUP BY status"
            ).fetchall()
            fill_row = self._connection.execute(
                "SELECT COUNT(*) FROM paper_fills"
            ).fetchone()
            position_rows = self._connection.execute(
                "SELECT symbol, side, quantity FROM paper_fills ORDER BY symbol"
            ).fetchall()
            cash_rows = self._connection.execute(
                "SELECT side, notional, commission, tax FROM paper_fills"
            ).fetchall()
        except sqlite3.Error as error:
            raise RuntimeError("SQLite metrics query failed") from error
        return MetricsSnapshot(
            generated_at=generated_at,
            latest_run=_run_from_row(latest_row) if latest_row else None,
            run_counts=_run_counts(count_rows),
            paper_fill_count=int(fill_row[0]) if fill_row else 0,
            position_quantities=_sqlite_positions(position_rows),
            paper_cash_change=_paper_cash_change(cash_rows),
        )


class PostgresMetricsStore:
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
                **{name: connection_parameters[name] for name in required},
                options="-c default_transaction_read_only=on",
                connect_timeout=5,
            )
        except self._database_error as error:
            raise RuntimeError("PostgreSQL metrics connection failed") from error

    def close(self) -> None:
        self._connection.close()

    def snapshot(self, *, generated_at: datetime) -> MetricsSnapshot:
        _require_aware(generated_at)
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(LATEST_RUN_SQL)
                latest_row = cursor.fetchone()
                cursor.execute(
                    "SELECT status, COUNT(*) FROM paper_cycle_runs GROUP BY status"
                )
                count_rows = cursor.fetchall()
                cursor.execute("SELECT COUNT(*) FROM paper_fills")
                fill_row = cursor.fetchone()
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
                    ORDER BY symbol
                    """
                )
                position_rows = cursor.fetchall()
                cursor.execute(
                    "SELECT side, notional, commission, tax FROM paper_fills"
                )
                cash_rows = cursor.fetchall()
        except self._database_error as error:
            self._connection.rollback()
            raise RuntimeError("PostgreSQL metrics query failed") from error
        self._connection.rollback()
        return MetricsSnapshot(
            generated_at=generated_at,
            latest_run=_run_from_row(latest_row) if latest_row else None,
            run_counts=_run_counts(count_rows),
            paper_fill_count=int(fill_row[0]) if fill_row else 0,
            position_quantities={
                str(symbol): Decimal(str(quantity))
                for symbol, quantity in position_rows
            },
            paper_cash_change=_paper_cash_change(cash_rows),
        )


def open_metrics_store(
    *,
    postgres_parameters: Mapping[str, str | int] | None,
    sqlite_path: str,
) -> MetricsStore:
    if postgres_parameters:
        return PostgresMetricsStore(postgres_parameters)
    return SqliteMetricsStore(sqlite_path)


class MetricsService:
    def __init__(
        self,
        store: MetricsStore,
        *,
        initial_cash: Decimal = Decimal(1000000),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        self._store = store
        self._initial_cash = initial_cash
        self._clock = clock or (lambda: datetime.now(UTC))

    def render(self) -> str:
        return render_prometheus(
            self._store.snapshot(generated_at=self._clock()),
            initial_cash=self._initial_cash,
        )


def render_prometheus(
    snapshot: MetricsSnapshot, *, initial_cash: Decimal = Decimal(1000000)
) -> str:
    latest = snapshot.latest_run
    present = int(latest is not None)
    success = int(latest is not None and latest.status == "succeeded")
    available_cash = initial_cash + snapshot.paper_cash_change
    deployed_cash = -snapshot.paper_cash_change
    lines = [
        "# HELP toss_trader_up Metrics database query succeeded.",
        "# TYPE toss_trader_up gauge",
        "toss_trader_up 1",
        "# HELP toss_trader_metrics_generated_timestamp_seconds Metrics generation time.",
        "# TYPE toss_trader_metrics_generated_timestamp_seconds gauge",
        (
            "toss_trader_metrics_generated_timestamp_seconds "
            f"{_number(snapshot.generated_at.timestamp())}"
        ),
        "# HELP toss_trader_cycle_runs_total Completed paper cycle runs by status.",
        "# TYPE toss_trader_cycle_runs_total counter",
    ]
    lines.extend(
        f'toss_trader_cycle_runs_total{{status="{status}"}} '
        f"{snapshot.run_counts.get(status, 0)}"
        for status in COMPLETED_RUN_STATUSES
    )
    lines.extend(
        [
            "# HELP toss_trader_cycle_runs_running Currently running paper cycles.",
            "# TYPE toss_trader_cycle_runs_running gauge",
            (f"toss_trader_cycle_runs_running {snapshot.run_counts.get('running', 0)}"),
            "# HELP toss_trader_cycle_last_present Whether a completed cycle exists.",
            "# TYPE toss_trader_cycle_last_present gauge",
            f"toss_trader_cycle_last_present {present}",
            "# HELP toss_trader_cycle_last_success Whether latest cycle succeeded.",
            "# TYPE toss_trader_cycle_last_success gauge",
            f"toss_trader_cycle_last_success {success}",
        ]
    )
    _append_latest(lines, latest)
    lines.extend(
        [
            "# HELP toss_trader_paper_fills_total Persisted paper fills.",
            "# TYPE toss_trader_paper_fills_total counter",
            f"toss_trader_paper_fills_total {snapshot.paper_fill_count}",
            "# HELP toss_trader_paper_initial_cash_krw Paper starting cash in KRW.",
            "# TYPE toss_trader_paper_initial_cash_krw gauge",
            f"toss_trader_paper_initial_cash_krw {_number(initial_cash)}",
            "# HELP toss_trader_paper_available_cash_krw Available paper cash in KRW.",
            "# TYPE toss_trader_paper_available_cash_krw gauge",
            f"toss_trader_paper_available_cash_krw {_number(available_cash)}",
            "# HELP toss_trader_paper_deployed_cash_krw Net paper cash deployed in KRW.",
            "# TYPE toss_trader_paper_deployed_cash_krw gauge",
            f"toss_trader_paper_deployed_cash_krw {_number(deployed_cash)}",
            "# HELP toss_trader_paper_position_quantity Open paper quantity by symbol.",
            "# TYPE toss_trader_paper_position_quantity gauge",
        ]
    )
    lines.extend(
        f'toss_trader_paper_position_quantity{{symbol="{_label(symbol)}"}} '
        f"{_number(quantity)}"
        for symbol, quantity in sorted(snapshot.position_quantities.items())
    )
    return "\n".join(lines) + "\n"


def _append_latest(lines: list[str], latest: PaperCycleRun | None) -> None:
    metrics: tuple[tuple[str, str, Decimal | int | float], ...]
    if latest is None:
        metrics = (
            ("finished_timestamp_seconds", "Latest cycle finish time.", 0),
            ("duration_seconds", "Latest cycle duration.", 0),
            ("symbols", "Latest cycle symbol count.", 0),
            ("signals", "Latest cycle signal count.", 0),
            ("fills", "Latest cycle paper fill count.", 0),
            ("failed", "Latest cycle failed symbol count.", 0),
            ("consecutive_api_errors", "Latest API error streak.", 0),
            ("daily_return_ratio", "Latest paper daily return ratio.", 0),
        )
    else:
        duration = (
            max(
                Decimal(0),
                Decimal(str((latest.finished_at - latest.started_at).total_seconds())),
            )
            if latest.finished_at is not None
            else Decimal(0)
        )
        metrics = (
            (
                "finished_timestamp_seconds",
                "Latest cycle finish time.",
                latest.finished_at.timestamp() if latest.finished_at else 0,
            ),
            ("duration_seconds", "Latest cycle duration.", duration),
            ("symbols", "Latest cycle symbol count.", latest.symbol_count),
            ("signals", "Latest cycle signal count.", latest.signal_count),
            ("fills", "Latest cycle paper fill count.", latest.fill_count),
            ("failed", "Latest cycle failed symbol count.", latest.failed_count),
            (
                "consecutive_api_errors",
                "Latest API error streak.",
                latest.consecutive_api_errors,
            ),
            (
                "daily_return_ratio",
                "Latest paper daily return ratio.",
                latest.daily_return_rate,
            ),
        )
    for suffix, help_text, value in metrics:
        name = f"toss_trader_cycle_last_{suffix}"
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
        lines.append(f"{name} {_number(value)}")


def create_metrics_server(
    *, host: str, port: int, render: Callable[[], str]
) -> HTTPServer:
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, content_type, body = metrics_response(self.path, render)
            self._send(status, body, content_type)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return HTTPServer((host, port), MetricsHandler)


def metrics_response(path: str, render: Callable[[], str]) -> tuple[int, str, bytes]:
    normalized = urlsplit(path).path
    if normalized not in {"/metrics", "/healthz"}:
        return 404, "text/plain; charset=utf-8", b"not found\n"
    try:
        metrics = render()
    except (OSError, RuntimeError, TypeError, ValueError):
        return 503, "text/plain; charset=utf-8", b"metrics unavailable\n"
    if normalized == "/healthz":
        return 200, "text/plain; charset=utf-8", b"ok\n"
    return (
        200,
        "text/plain; version=0.0.4; charset=utf-8",
        metrics.encode(),
    )


def serve_metrics(*, host: str, port: int, render: Callable[[], str]) -> None:
    with create_metrics_server(host=host, port=port, render=render) as server:
        server.serve_forever()


def _run_counts(rows: Sequence[Sequence[object]]) -> dict[str, int]:
    counts = {status: 0 for status in RUN_STATUSES}
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def _sqlite_positions(rows: Sequence[Sequence[object]]) -> dict[str, Decimal]:
    positions: dict[str, Decimal] = {}
    for symbol, side, quantity in rows:
        signed = Decimal(str(quantity)) if side == "BUY" else -Decimal(str(quantity))
        positions[str(symbol)] = positions.get(str(symbol), Decimal(0)) + signed
    return {symbol: quantity for symbol, quantity in positions.items() if quantity}


def _paper_cash_change(rows: Sequence[Sequence[object]]) -> Decimal:
    change = Decimal(0)
    for side, notional, commission, tax in rows:
        value = Decimal(str(notional))
        costs = Decimal(str(commission)) + Decimal(str(tax))
        if side == "BUY":
            change -= value + costs
        elif side == "SELL":
            change += value - costs
        else:
            raise ValueError(f"unsupported paper fill side: {side}")
    return change


def _run_from_row(row: Sequence[object]) -> PaperCycleRun:
    return PaperCycleRun(
        run_id=str(row[0]),
        started_at=_datetime(row[1]),
        finished_at=_datetime(row[2]) if row[2] is not None else None,
        status=str(row[3]),
        interval=str(row[4]),
        symbol_count=int(row[5]),
        signal_count=int(row[6]),
        fill_count=int(row[7]),
        failed_count=int(row[8]),
        consecutive_api_errors=int(row[9]),
        daily_return_rate=Decimal(str(row[10])),
        error_message=str(row[11]) if row[11] is not None else None,
    )


def _datetime(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must include a timezone offset")


def _number(value: Decimal | float) -> str:
    if isinstance(value, float):
        return format(value, ".6f").rstrip("0").rstrip(".")
    return str(value)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
