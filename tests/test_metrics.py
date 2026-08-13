import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self

from toss_trader.cycle_state import SqliteCycleStateStore
from toss_trader.metrics import (
    MetricsService,
    PostgresMetricsStore,
    SqliteMetricsStore,
    metrics_response,
)
from toss_trader.models import Side, TradeSignal
from toss_trader.paper import PaperLedger

STARTED = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)
FINISHED = STARTED + timedelta(seconds=2)


def seed_sqlite(database_path: str) -> None:
    ledger = PaperLedger(database_path)
    ledger.execute(
        TradeSignal(
            signal_id="metrics-buy",
            symbol="005930",
            side=Side.BUY,
            reference_price=Decimal(70000),
            quantity=Decimal("1.5"),
            reason="metrics fixture",
        ),
        executed_at=STARTED,
    )
    ledger.close()
    state = SqliteCycleStateStore(database_path)
    run_id = state.start_run(started_at=STARTED, interval="1d", symbol_count=2)
    state.finish_run(
        run_id=run_id,
        finished_at=FINISHED,
        status="partial_failure",
        signal_count=1,
        fill_count=0,
        failed_count=1,
        consecutive_api_errors=3,
        daily_return_rate=Decimal("-0.04"),
        error_message="AAPL: timeout",
    )
    state.close()


class SqliteMetricsStoreTest(unittest.TestCase):
    def test_reads_cycle_fill_and_position_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            seed_sqlite(database_path)
            store = SqliteMetricsStore(database_path)

            snapshot = store.snapshot(
                generated_at=datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
            )
            store.close()

        assert snapshot.latest_run is not None
        self.assertEqual(snapshot.latest_run.status, "partial_failure")
        self.assertEqual(snapshot.latest_run.daily_return_rate, Decimal("-0.04"))
        self.assertEqual(snapshot.run_counts["partial_failure"], 1)
        self.assertEqual(snapshot.paper_fill_count, 1)
        self.assertEqual(snapshot.position_quantities, {"005930": Decimal("1.5")})
        self.assertEqual(snapshot.paper_cash_change, Decimal("-105015.0"))

    def test_renders_prometheus_exposition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            seed_sqlite(database_path)
            store = SqliteMetricsStore(database_path)
            service = MetricsService(
                store,
                clock=lambda: datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
            )

            output = service.render()
            store.close()

        self.assertIn("toss_trader_up 1", output)
        self.assertIn(
            'toss_trader_cycle_runs_total{status="partial_failure"} 1', output
        )
        self.assertIn("toss_trader_cycle_runs_running 0", output)
        self.assertNotIn('toss_trader_cycle_runs_total{status="running"}', output)
        self.assertIn("toss_trader_cycle_last_daily_return_ratio -0.04", output)
        self.assertIn("toss_trader_cycle_last_consecutive_api_errors 3", output)
        self.assertIn(
            'toss_trader_paper_position_quantity{symbol="005930"} 1.5', output
        )
        self.assertIn("toss_trader_paper_initial_cash_krw 1000000", output)
        self.assertIn("toss_trader_paper_available_cash_krw 894985.0", output)
        self.assertIn("toss_trader_paper_deployed_cash_krw 105015.0", output)


class FakeMetricsCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.last_query = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str) -> None:
        self.last_query = query
        self.executed.append(query)

    def fetchone(self) -> tuple | None:
        if "FROM paper_cycle_runs" in self.last_query:
            return (
                "00000000-0000-0000-0000-000000000001",
                STARTED,
                FINISHED,
                "succeeded",
                "1d",
                1,
                0,
                0,
                0,
                0,
                Decimal("0.01"),
                None,
            )
        if "COUNT(*) FROM paper_fills" in self.last_query:
            return (1,)
        return None

    def fetchall(self) -> list[tuple]:
        if "GROUP BY status" in self.last_query:
            return [("succeeded", 1)]
        if "GROUP BY symbol" in self.last_query:
            return [("005930", Decimal(1))]
        if (
            "SELECT side, notional, commission, tax FROM paper_fills"
            in self.last_query
        ):
            return [("BUY", Decimal(71000), Decimal(10), Decimal(0))]
        return []


class FakeMetricsConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeMetricsCursor()
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeMetricsCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class PostgresMetricsStoreTest(unittest.TestCase):
    def test_forces_read_only_connection_and_ends_snapshot_transaction(self) -> None:
        connection = FakeMetricsConnection()
        received: dict[str, object] = {}

        def connect(**kwargs: object) -> FakeMetricsConnection:
            received.update(kwargs)
            return connection

        store = PostgresMetricsStore(
            {
                "host": "postgres.internal",
                "port": 5432,
                "user": "trader",
                "password": "secret@:/value",
                "dbname": "toss_trader",
            },
            connect=connect,
        )

        snapshot = store.snapshot(generated_at=FINISHED)
        store.close()

        self.assertEqual(received["options"], "-c default_transaction_read_only=on")
        self.assertEqual(received["connect_timeout"], 5)
        self.assertEqual(snapshot.paper_fill_count, 1)
        self.assertEqual(snapshot.paper_cash_change, Decimal(-71010))
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)


class MetricsHttpServerTest(unittest.TestCase):
    def test_builds_prometheus_http_response(self) -> None:
        status, content_type, body = metrics_response(
            "/metrics?source=test", lambda: "test_metric 1\n"
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, b"test_metric 1\n")
        self.assertEqual(content_type, "text/plain; version=0.0.4; charset=utf-8")

    def test_health_fails_closed_when_database_query_fails(self) -> None:
        def fail() -> str:
            raise RuntimeError("database unavailable")

        status, _, body = metrics_response("/healthz", fail)

        self.assertEqual(status, 503)
        self.assertEqual(body, b"metrics unavailable\n")


if __name__ == "__main__":
    unittest.main()
