import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

from toss_trader.cycle_state import PostgresCycleStateStore, SqliteCycleStateStore

STARTED = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


class SqliteCycleStateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SqliteCycleStateStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_persists_completed_run_and_error_streak(self) -> None:
        run_id = self.store.start_run(started_at=STARTED, interval="1d", symbol_count=2)
        self.store.finish_run(
            run_id=run_id,
            finished_at=STARTED + timedelta(seconds=2),
            status="partial_failure",
            signal_count=1,
            fill_count=0,
            failed_count=1,
            consecutive_api_errors=3,
            daily_return_rate=Decimal("-0.012"),
            error_message="005930: timeout",
        )

        latest = self.store.latest_run()

        assert latest is not None
        self.assertEqual(latest.run_id, run_id)
        self.assertEqual(latest.status, "partial_failure")
        self.assertEqual(latest.consecutive_api_errors, 3)
        self.assertEqual(latest.daily_return_rate, Decimal("-0.012"))
        self.assertEqual(self.store.latest_consecutive_api_errors(), 3)

    def test_running_row_does_not_replace_last_completed_streak(self) -> None:
        completed = self.store.start_run(
            started_at=STARTED, interval="1d", symbol_count=1
        )
        self.store.finish_run(
            run_id=completed,
            finished_at=STARTED + timedelta(seconds=1),
            status="failed",
            signal_count=0,
            fill_count=0,
            failed_count=1,
            consecutive_api_errors=2,
            daily_return_rate=Decimal(0),
            error_message="timeout",
        )
        self.store.start_run(
            started_at=STARTED + timedelta(minutes=1),
            interval="1d",
            symbol_count=1,
        )

        self.assertEqual(self.store.latest_consecutive_api_errors(), 2)


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: object | None = None) -> None:
        self.executed.append((query, params))


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.commits = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class PostgresCycleStateStoreTest(unittest.TestCase):
    def test_initializes_and_writes_parameterized_run(self) -> None:
        connection = FakeConnection()

        store = PostgresCycleStateStore(
            {
                "host": "postgres.internal",
                "port": 5432,
                "user": "trader",
                "password": "secret@:/value",
                "dbname": "toss_trader",
            },
            connect=lambda **kwargs: connection,
        )
        run_id = store.start_run(started_at=STARTED, interval="1d", symbol_count=1)
        store.finish_run(
            run_id=run_id,
            finished_at=STARTED + timedelta(seconds=1),
            status="succeeded",
            signal_count=0,
            fill_count=0,
            failed_count=0,
            consecutive_api_errors=0,
            daily_return_rate=Decimal(0),
            error_message=None,
        )
        store.close()

        statements = connection.cursor_instance.executed
        self.assertIn("TIMESTAMPTZ", statements[0][0])
        self.assertIn("VALUES (%s, %s", statements[3][0])
        self.assertIn("WHERE run_id = %s", statements[4][0])
        self.assertEqual(connection.commits, 3)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
