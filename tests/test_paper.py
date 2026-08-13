import unittest
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from toss_trader.models import Side, TradeSignal
from toss_trader.paper import DuplicatePaperOrder, PaperLedger, PostgresPaperLedger


class PaperLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = PaperLedger(":memory:")

    def tearDown(self) -> None:
        self.ledger.close()

    def test_records_fill_and_rejects_duplicate_signal(self) -> None:
        trade_signal = TradeSignal(
            signal_id="signal-1",
            symbol="005930",
            side=Side.BUY,
            reference_price=Decimal(71000),
            quantity=Decimal(2),
            reason="test",
        )
        executed_at = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

        fill = self.ledger.execute(trade_signal, executed_at=executed_at)

        self.assertEqual(fill.notional, Decimal(142000))
        self.assertEqual(fill.commission, Decimal(21))
        self.assertEqual(fill.tax, Decimal(0))
        self.assertEqual(fill.total_cost, Decimal(21))
        self.assertEqual(self.ledger.daily_buy_count(executed_at.date()), 1)
        self.assertEqual(self.ledger.position_notional("005930"), Decimal(142000))
        self.assertEqual(
            self.ledger.cash_balance(Decimal(1000000)), Decimal(857979)
        )
        with self.assertRaises(DuplicatePaperOrder):
            self.ledger.execute(trade_signal, executed_at=executed_at)

    def test_sell_reduces_position_notional(self) -> None:
        when = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
        self.ledger.execute(
            TradeSignal(
                signal_id="buy-1",
                symbol="AAPL",
                side=Side.BUY,
                reference_price=Decimal(200),
                quantity=Decimal(3),
                reason="buy",
            ),
            executed_at=when,
        )
        self.ledger.execute(
            TradeSignal(
                signal_id="sell-1",
                symbol="AAPL",
                side=Side.SELL,
                reference_price=Decimal(210),
                quantity=Decimal(1),
                reason="sell",
            ),
            executed_at=when,
        )

        self.assertEqual(
            self.ledger.position_notional("AAPL", mark_price=Decimal(210)),
            Decimal(420),
        )
        self.assertEqual(self.ledger.position_quantity("AAPL"), Decimal(2))
        self.assertEqual(self.ledger.cash_balance(Decimal(1000)), Decimal("609.19"))

    def test_domestic_sell_charges_2026_transaction_tax(self) -> None:
        fill = self.ledger.execute(
            TradeSignal(
                signal_id="kr-sell-1",
                symbol="005930",
                side=Side.SELL,
                reference_price=Decimal(71000),
                quantity=Decimal(2),
                reason="cost test",
            ),
            executed_at=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
        )

        self.assertEqual(fill.commission, Decimal(21))
        self.assertEqual(fill.tax, Decimal(284))

    def test_us_order_up_to_ten_dollars_has_no_commission(self) -> None:
        fill = self.ledger.execute(
            TradeSignal(
                signal_id="us-small-buy",
                symbol="AAPL",
                side=Side.BUY,
                reference_price=Decimal(10),
                quantity=Decimal(1),
                reason="fee waiver test",
            )
        )

        self.assertEqual(fill.commission, Decimal(0))
        self.assertEqual(fill.tax, Decimal(0))

    def test_records_and_queries_automation_run_tokens(self) -> None:
        started_at = datetime(2026, 8, 12, 8, 30, tzinfo=UTC)
        run_id = self.ledger.record_automation_run(
            run_type="market_scan",
            status="succeeded",
            stage="completed",
            started_at=started_at,
            finished_at=datetime(2026, 8, 12, 8, 30, 2, tzinfo=UTC),
            prompt_tokens=436,
            completion_tokens=6,
            total_tokens=442,
            details={"markets": 2, "candidates": 1, "errors": 0},
        )

        runs = self.ledger.recent_automation_runs(run_type="market_scan")

        self.assertEqual(runs[0]["runId"], run_id)
        self.assertEqual(runs[0]["durationMs"], 2000)
        self.assertEqual(runs[0]["totalTokens"], 442)
        self.assertEqual(runs[0]["details"]["errors"], 0)


class FakePaperCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: object | None = None) -> None:
        self.executed.append((query, params))


class FakePaperConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakePaperCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakePaperCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class PostgresPaperLedgerTest(unittest.TestCase):
    def test_initializes_schema_and_records_parameterized_fill(self) -> None:
        connection = FakePaperConnection()
        received: dict = {}

        def connect(**kwargs: object) -> FakePaperConnection:
            received.update(kwargs)
            return connection

        ledger = PostgresPaperLedger(
            {
                "host": "postgres.internal",
                "port": 5432,
                "user": "trader",
                "password": "secret@:/value",
                "dbname": "toss_trader",
            },
            connect=connect,
        )
        fill = ledger.execute(
            TradeSignal(
                signal_id="pg-signal-1",
                symbol="005930",
                side=Side.BUY,
                reference_price=Decimal(71000),
                quantity=Decimal(1),
                reason="postgres smoke",
            ),
            executed_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
        )
        ledger.close()

        self.assertEqual(fill.notional, Decimal(71000))
        self.assertEqual(fill.commission, Decimal(10))
        self.assertEqual(fill.tax, Decimal(0))
        self.assertIn("TIMESTAMPTZ", connection.cursor_instance.executed[0][0])
        insert, params = next(
            (query, params)
            for query, params in connection.cursor_instance.executed
            if "INSERT INTO paper_fills" in query
        )
        self.assertIn("VALUES (%s, %s", insert)
        assert isinstance(params, tuple)
        self.assertEqual(params[1:5], ("legacy", "pg-signal-1", "005930", "BUY"))
        self.assertEqual(received["password"], "secret@:/value")
        self.assertEqual(connection.commits, 2)
        self.assertTrue(connection.closed)

    def test_skips_schema_ddl_for_runtime_audit_connection(self) -> None:
        connection = FakePaperConnection()

        ledger = PostgresPaperLedger(
            {
                "host": "postgres.internal",
                "port": 5432,
                "user": "trader",
                "password": "secret",
                "dbname": "toss_trader",
            },
            connect=lambda **_: connection,
            initialize_schema=False,
        )
        ledger.record_automation_run(
            run_type="n8n_flow",
            status="succeeded",
            stage="risk-manager-evaluate",
            started_at=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 13, 9, 0, 1, tzinfo=UTC),
        )

        queries = [query for query, _ in connection.cursor_instance.executed]
        self.assertEqual(len(queries), 1)
        self.assertIn("INSERT INTO automation_run_logs", queries[0])
        self.assertNotIn("CREATE TABLE", queries[0])
        self.assertNotIn("ALTER TABLE", queries[0])
        self.assertEqual(connection.commits, 1)


if __name__ == "__main__":
    unittest.main()
