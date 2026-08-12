import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from toss_trader.cli import build_parser, main
from toss_trader.cycle_state import SqliteCycleStateStore
from toss_trader.models import Side, TradeSignal
from toss_trader.paper import PaperLedger


class MetricsCliTest(unittest.TestCase):
    def test_metrics_command_renders_from_read_only_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            ledger = PaperLedger(database_path)
            ledger.execute(
                TradeSignal(
                    signal_id="cli-metrics-buy",
                    symbol="005930",
                    side=Side.BUY,
                    reference_price=Decimal(70000),
                    quantity=Decimal(1),
                    reason="fixture",
                ),
                executed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
            ledger.close()
            state = SqliteCycleStateStore(database_path)
            run_id = state.start_run(
                started_at=datetime(2026, 8, 12, tzinfo=UTC),
                interval="1d",
                symbol_count=1,
            )
            state.finish_run(
                run_id=run_id,
                finished_at=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC),
                status="succeeded",
                signal_count=0,
                fill_count=0,
                failed_count=0,
                consecutive_api_errors=0,
                daily_return_rate=Decimal("0.01"),
                error_message=None,
            )
            state.close()
            output = io.StringIO()

            with (
                patch.dict(
                    "os.environ",
                    {"PAPER_DB_PATH": database_path},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(["metrics"])

        self.assertEqual(exit_code, 0)
        self.assertIn("toss_trader_up 1", output.getvalue())

    def test_serve_metrics_accepts_listener_overrides(self) -> None:
        args = build_parser().parse_args(
            ["serve-metrics", "--host", "127.0.0.1", "--port", "9200"]
        )

        self.assertEqual(args.command, "serve-metrics")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9200)

    def test_config_reports_metrics_listener_without_secrets(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {"METRICS_HOST": "127.0.0.1", "METRICS_PORT": "9200"},
                clear=True,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(["config"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["metricsHost"], "127.0.0.1")
        self.assertEqual(payload["metricsPort"], 9200)

    def test_serve_automation_command_has_no_live_order_options(self) -> None:
        args = build_parser().parse_args(["serve-automation"])

        self.assertEqual(args.command, "serve-automation")
        self.assertFalse(hasattr(args, "live"))

if __name__ == "__main__":
    unittest.main()
