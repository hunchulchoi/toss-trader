import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Self

from toss_trader.models import Side, TradeSignal, V2PositionPlan
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
        self.assertEqual(self.ledger.position_notional("005930"), Decimal(142021))
        self.assertEqual(
            self.ledger.cash_balance(Decimal(1000000)), Decimal(857979)
        )
        with self.assertRaises(DuplicatePaperOrder):
            self.ledger.execute(trade_signal, executed_at=executed_at)

    def test_persists_and_marks_v2_position_plan(self) -> None:
        opened_at = datetime(2026, 8, 18, 0, 1, tzinfo=UTC)
        plan = V2PositionPlan(
            symbol="005930",
            cluster_id="semiconductor",
            setup_session=date(2026, 8, 17),
            setups=("pullback", "flow-reversal"),
            quantity=Decimal(3),
            entry_price=Decimal(10005),
            stop_price=Decimal(9400),
            planned_heat=Decimal("1980.5"),
            ma50=Decimal(9700),
            opened_at=opened_at,
        )

        self.ledger.upsert_v2_position_plan(plan)

        self.assertEqual(self.ledger.v2_position_plan("005930"), plan)
        self.assertEqual(self.ledger.v2_position_plans(), {"005930": plan})

        triggered_at = datetime(2026, 8, 18, 1, 5, tzinfo=UTC)
        self.ledger.mark_v2_exit_pending(
            "005930", reason="hard-stop", triggered_at=triggered_at
        )
        pending = self.ledger.v2_position_plan("005930")
        assert pending is not None
        self.assertEqual(pending.exit_pending_reason, "hard-stop")
        self.assertEqual(pending.exit_triggered_at, triggered_at)

        self.ledger.delete_v2_position_plan("005930")
        self.assertIsNone(self.ledger.v2_position_plan("005930"))

    def test_v2_position_plan_rejects_inconsistent_pending_exit(self) -> None:
        with self.assertRaisesRegex(ValueError, "pending exit"):
            V2PositionPlan(
                symbol="005930",
                cluster_id="semiconductor",
                setup_session=date(2026, 8, 17),
                setups=("pullback",),
                quantity=Decimal(1),
                entry_price=Decimal(10000),
                stop_price=Decimal(9000),
                planned_heat=Decimal(1000),
                ma50=Decimal(9500),
                opened_at=datetime(2026, 8, 18, tzinfo=UTC),
                exit_pending_reason="hard-stop",
            )

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

    def test_average_cost_tracks_partial_sale_and_net_realized_pnl(self) -> None:
        when = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
        for signal_id, side, price, quantity in (
            ("avg-buy-1", Side.BUY, Decimal(10000), Decimal(10)),
            ("avg-buy-2", Side.BUY, Decimal(12000), Decimal(10)),
            ("avg-sell", Side.SELL, Decimal(15000), Decimal(5)),
        ):
            self.ledger.execute(
                TradeSignal(
                    signal_id=signal_id,
                    symbol="005930",
                    side=side,
                    reference_price=price,
                    quantity=quantity,
                    reason="accounting test",
                ),
                executed_at=when,
            )

        accounting = self.ledger.position_accounting("005930")

        self.assertEqual(accounting.quantity, Decimal(15))
        self.assertEqual(accounting.cost_basis, Decimal("165024.75"))
        self.assertEqual(accounting.realized_pnl, Decimal("19830.75"))
        self.assertEqual(accounting.commission, Decimal(44))
        self.assertEqual(accounting.tax, Decimal(150))

    def test_round_trip_clears_cost_basis(self) -> None:
        when = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
        for signal_id, side, price in (
            ("round-buy", Side.BUY, Decimal(10000)),
            ("round-sell", Side.SELL, Decimal(11000)),
        ):
            self.ledger.execute(
                TradeSignal(
                    signal_id=signal_id,
                    symbol="005930",
                    side=side,
                    reference_price=price,
                    quantity=Decimal(10),
                    reason="round trip",
                ),
                executed_at=when,
            )

        accounting = self.ledger.position_accounting("005930")

        self.assertEqual(accounting.quantity, Decimal(0))
        self.assertEqual(accounting.cost_basis, Decimal(0))
        self.assertEqual(accounting.realized_pnl, Decimal(9749))

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
