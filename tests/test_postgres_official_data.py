from __future__ import annotations

import unittest

from toss_trader.official_data import OfficialDataRepository


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rowcount = 0

    def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        materialized = list(rows)
        self.connection.executed_many.append((sql, materialized))
        self.rowcount = len(materialized)

    def close(self) -> None:
        return


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.executed_many: list[tuple[str, list[tuple[object, ...]]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> FakeCursor:
        self.executed.append((sql, parameters))
        return FakeCursor(self)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class PostgresOfficialDataRepositoryTest(unittest.TestCase):
    def test_flow_insert_creates_month_partition_and_is_idempotent(self) -> None:
        connection = FakeConnection()
        parameters: dict[str, str | int] = {
            "host": "postgres.internal",
            "port": 5431,
            "user": "trader",
            "password": "secret",
            "dbname": "toss_trader",
        }

        repository = OfficialDataRepository(
            "unused.db",
            postgres_parameters=parameters,
            connect=lambda **_kwargs: connection,
            database_error=RuntimeError,
        )
        inserted = repository.insert_flow_rows(
            [
                {
                    "symbol": "005930",
                    "session_date": "2026-08-18",
                    "session_index": 10,
                    "available_at": "2026-08-19T00:00:00+00:00",
                    "foreign_net_buy": "1",
                    "institutional_net_buy": "2",
                    "trading_value": "3",
                    "source": "krx:manual-csv",
                    "source_record_id": "2026-08-18:005930",
                    "retrieved_at": "2026-08-19T00:00:00+00:00",
                    "payload_hash": "hash",
                }
            ]
        )

        self.assertEqual(repository.backend, "postgresql")
        self.assertEqual(inserted, 1)
        self.assertTrue(
            any(
                "market_flow_pit_v2_y2026m08" in sql
                and "PARTITION OF market_flow_pit_v2" in sql
                for sql, _parameters in connection.executed
            )
        )
        insert_sql, rows = connection.executed_many[-1]
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)
        self.assertIn("%s", insert_sql)
        self.assertEqual(len(rows), 1)

        repository.close()
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
