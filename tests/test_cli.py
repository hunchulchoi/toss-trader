import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from toss_trader.cli import (
    _approved_hunter_candidates,
    _collect_intraday_sample,
    _cycle_snapshot_to_dict,
    _extend_cycle_snapshot,
    _hermes_candidate_snapshot,
    _hunter_entry_payload,
    _intraday_backfill_start_cursor,
    _momentum_collection_symbols,
    _paper_session_accounting,
    _record_setup_parameter_shadow_once,
    _recorded_momentum_symbols,
    _recorded_setup_parameter_symbols,
    _seoul_day_window,
    _session_candle_count,
    _setup_parameter_shadow_recorded,
    build_parser,
    main,
)
from toss_trader.cycle import PaperCycleSnapshot
from toss_trader.cycle_state import SqliteCycleStateStore
from toss_trader.execution import PaperTradingService
from toss_trader.market_data import CollectionResult
from toss_trader.models import Candle, Side, TradeSignal
from toss_trader.paper import PaperLedger
from toss_trader.repository import SqliteMarketRepository
from toss_trader.risk import RiskLimits, RiskManager


class SessionAccountingPayloadTest(unittest.TestCase):
    def test_distinguishes_current_cycle_from_session_fills(self) -> None:
        ledger = PaperLedger(":memory:", portfolio_id="rule")
        try:
            for signal_id, symbol, executed_at in (
                (
                    "before-open",
                    "005930",
                    datetime(2026, 8, 25, 23, 50, tzinfo=UTC),
                ),
                (
                    "session-buy",
                    "000660",
                    datetime(2026, 8, 26, 0, 10, tzinfo=UTC),
                ),
            ):
                ledger.execute(
                    TradeSignal(
                        signal_id=signal_id,
                        symbol=symbol,
                        side=Side.BUY,
                        reference_price=Decimal(10000),
                        quantity=Decimal(1),
                        reason="accounting-test",
                    ),
                    executed_at=executed_at,
                )
            now = datetime(2026, 8, 26, 4, 42, tzinfo=UTC)
            ledger.record_daily_equity_baseline(
                captured_at=now,
                equity=Decimal("1002254.72"),
            )

            payload = _paper_session_accounting(
                ledger,
                now=now,
                current_cycle_fills=0,
                initial_cash=Decimal(1000000),
                cash_balance=ledger.cash_balance(Decimal(1000000)),
                equity=Decimal("1000664.72"),
                symbol_names={"005930": "삼성전자", "000660": "SK하이닉스"},
            )

            self.assertEqual(payload["currentCycleFills"], 0)
            self.assertEqual(payload["sessionBuyFills"], 1)
            self.assertEqual(payload["sessionSellFills"], 0)
            self.assertEqual(
                [row["symbol"] for row in payload["sessionFills"]],
                ["000660"],
            )
            self.assertEqual(payload["dailyBaselineEquity"], "1002254.72")
            self.assertEqual(
                {row["symbol"] for row in payload["positions"]},
                {"005930", "000660"},
            )
        finally:
            ledger.close()


class IntradaySampleCollectionTest(unittest.TestCase):
    def test_session_count_reads_enough_history_for_week_backfill(self) -> None:
        class Repository:
            def latest_candles(self, symbol: str, interval: str, *, limit: int):
                self.call = (symbol, interval, limit)
                return [
                    Candle(
                        symbol=symbol,
                        interval=interval,
                        timestamp=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
                        open_price=Decimal(1),
                        high_price=Decimal(1),
                        low_price=Decimal(1),
                        close_price=Decimal(1),
                        volume=Decimal(1),
                        currency="KRW",
                    )
                ]

        repository = Repository()

        count = _session_candle_count(
            repository,  # type: ignore[arg-type]
            symbol="005930",
            session_day=datetime(2026, 8, 18, tzinfo=UTC).date(),
        )

        self.assertEqual(count, 1)
        self.assertEqual(repository.call, ("005930", "1m", 10_000))

    def test_intraday_review_window_starts_at_market_open(self) -> None:
        started, finished = _seoul_day_window(
            datetime(2026, 8, 21, 2, 50, tzinfo=UTC)
        )

        self.assertEqual(started, datetime(2026, 8, 21, 0, 0, tzinfo=UTC))
        self.assertEqual(finished, datetime(2026, 8, 21, 2, 50, tzinfo=UTC))

    def test_collects_thirty_bars_only_for_non_cycle_candidates(self) -> None:
        class Collector:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, int]] = []

            def collect(self, *, symbol: str, interval: str, count: int):
                self.calls.append((symbol, interval, count))
                if symbol == "035420":
                    raise OSError("temporary")
                return CollectionResult(symbol, interval, count, count, None)

        collector = Collector()

        result = _collect_intraday_sample(
            collector,  # type: ignore[arg-type]
            cycle_symbols=("005930", "000660"),
            collection_symbols=("005930", "000660", "207940", "035420"),
        )

        self.assertEqual(
            collector.calls,
            [("207940", "1m", 30), ("035420", "1m", 30)],
        )
        self.assertEqual(result["receivedCandles"], 30)
        self.assertEqual(result["upsertedCandles"], 30)
        self.assertEqual(result["failures"][0]["symbol"], "035420")

    def test_collects_benchmark_session_bars_outside_cycle(self) -> None:
        class Collector:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, int]] = []

            def collect(self, *, symbol: str, interval: str, count: int):
                self.calls.append((symbol, interval, count))
                return CollectionResult(symbol, interval, count, count, None)

        collector = Collector()
        result = _collect_intraday_sample(
            collector,  # type: ignore[arg-type]
            cycle_symbols=("278470",),
            collection_symbols=("278470",),
            extra_symbols=("069500", "229200"),
            extra_count=200,
        )

        self.assertEqual(
            collector.calls,
            [("069500", "1m", 200), ("229200", "1m", 200)],
        )
        self.assertEqual(result["extraSymbols"], ["069500", "229200"])

    def test_setup_parameter_shadow_audit_is_idempotent_and_queryable(self) -> None:
        class Ledger:
            def __init__(self) -> None:
                self.rows: list[dict[str, object]] = []

            def recent_automation_runs(self, **kwargs):
                return [
                    row
                    for row in self.rows
                    if row["runType"] == kwargs.get("run_type")
                ]

            def record_automation_run(self, **kwargs):
                run_id = f"parameter-{len(self.rows) + 1}"
                self.rows.append(
                    {
                        "runId": run_id,
                        "runType": kwargs["run_type"],
                        "status": kwargs["status"],
                        "details": dict(kwargs["details"]),
                    }
                )
                return run_id

        ledger = Ledger()
        observed_at = datetime(2026, 8, 27, 1, tzinfo=UTC)
        first = {
            "status": "evaluated",
            "ruleVersion": "setup-parameter-shadow-v1",
            "sessionDate": "2026-08-27",
        }
        second = dict(first)

        first_id = _record_setup_parameter_shadow_once(
            ledger, payload=first, observed_at=observed_at
        )
        second_id = _record_setup_parameter_shadow_once(
            ledger, payload=second, observed_at=observed_at
        )

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(ledger.rows), 1)
        self.assertEqual(ledger.rows[0]["status"], "succeeded")
        self.assertTrue(
            _setup_parameter_shadow_recorded(
                ledger, session_date="2026-08-27"
            )
        )
        self.assertFalse(first["cacheHit"])
        self.assertTrue(second["cacheHit"])
        ledger.rows[0]["details"]["rows"] = [
            {"symbol": "005930"},
            {"symbol": "000660"},
            {"symbol": "005930"},
        ]
        self.assertEqual(
            _recorded_setup_parameter_symbols(
                ledger, session_date="2026-08-27"
            ),
            ("005930", "000660"),
        )

    def test_setup_parameter_shadow_is_an_automation_query_type(self) -> None:
        args = build_parser().parse_args(
            ["automation-runs", "--type", "setup-parameter-shadow"]
        )

        self.assertEqual(args.type, "setup-parameter-shadow")

    def test_momentum_evaluation_collects_full_research_pool(self) -> None:
        self.assertEqual(
            _momentum_collection_symbols(
                research_pool=("AAA", "BBB", "CCC"),
                benchmark_symbols=("069500", "229200"),
                evaluation_due=True,
            ),
            ("069500", "229200", "AAA", "BBB", "CCC"),
        )
        self.assertEqual(
            _momentum_collection_symbols(
                research_pool=("AAA", "BBB"),
                benchmark_symbols=("069500", "229200"),
                evaluation_due=False,
            ),
            ("069500", "229200"),
        )

    def test_only_hunter_candidates_approved_by_hermes_become_entries(self) -> None:
        payload = {
            "status": "evaluated",
            "ruleVersion": "momentum-shadow-v2",
            "strategyInput": False,
            "shadowOnly": True,
            "sessionDate": "2026-08-25",
            "selected": [
                {"symbol": "AAA", "entryPrice": "100"},
                {"symbol": "BBB", "entryPrice": "200"},
            ],
            "hermes": {
                "status": "succeeded",
                "decisions": [
                    {"symbol": "AAA", "verdict": "approve"},
                    {"symbol": "BBB", "verdict": "watch"},
                ],
            },
        }

        promoted = _hunter_entry_payload(payload)
        self.assertEqual(
            _approved_hunter_candidates(promoted),
            {
                "AAA": {
                    "symbol": "AAA",
                    "entryPrice": "100",
                    "sessionDate": "2026-08-25",
                }
            },
        )

        promoted["paperOnly"] = False
        self.assertEqual(_approved_hunter_candidates(promoted), {})

    def test_refreshes_only_recorded_hunter_top_two_after_evaluation(self) -> None:
        class Ledger:
            def recent_automation_runs(self, **_kwargs):
                return [
                    {
                        "status": "succeeded",
                        "details": {
                            "sessionDate": "2026-08-25",
                            "ruleVersion": "momentum-shadow-v2",
                            "selected": [
                                {"symbol": "AAA"},
                                {"symbol": "BBB"},
                            ],
                        },
                    }
                ]

        self.assertEqual(
            _recorded_momentum_symbols(
                Ledger(), session_date="2026-08-25"
            ),
            ("AAA", "BBB"),
        )

    def test_builds_expanded_hermes_pool_and_keeps_held_symbol(self) -> None:
        base = PaperCycleSnapshot(
            evaluated_at=datetime(2026, 8, 20, 0, 5, tzinfo=UTC),
            symbols=("005930",),
            interval="1m",
            collections=(None,),
            signals=(None,),
            skips=(None,),
            errors=(None,),
            api_failed=False,
            new_buys_allowed=True,
            hunter_entry={"ruleVersion": "momentum-shadow-v2"},
        )

        research = _hermes_candidate_snapshot(
            base,
            ("005930", "000660"),
            failed_samples={"000660": "temporary"},
        )
        expanded = _extend_cycle_snapshot(research, ("035420",))

        self.assertEqual(expanded.symbols, ("005930", "000660", "035420"))
        self.assertEqual(expanded.errors, (None, "temporary", None))
        self.assertEqual(expanded.ma_states, (None, None, None))
        self.assertEqual(expanded.v2_candidates, ())
        encoded = _cycle_snapshot_to_dict(expanded)
        self.assertEqual(len(encoded["maStates"]), len(encoded["symbols"]))
        self.assertEqual(
            encoded["hunterEntry"],
            {"ruleVersion": "momentum-shadow-v2"},
        )


class MetricsCliTest(unittest.TestCase):
    def test_portfolio_backtest_supports_json_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_path = str(Path(directory) / "market.db")
            repository = SqliteMarketRepository(market_path)
            started_at = datetime(2026, 1, 1, tzinfo=UTC)
            for symbol, multiplier in (("005930", 1), ("000660", 2)):
                repository.upsert_candles(
                    [
                        Candle(
                            symbol=symbol,
                            interval="1d",
                            timestamp=started_at.replace(day=index + 1),
                            open_price=Decimal(close * multiplier),
                            high_price=Decimal(close * multiplier),
                            low_price=Decimal(close * multiplier),
                            close_price=Decimal(close * multiplier),
                            volume=Decimal(1000),
                            currency="KRW",
                        )
                        for index, close in enumerate([100, 100, 100, 120, 130])
                    ]
                )
            repository.close()

            outputs = []
            for output_format in ("json", "csv"):
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {"MARKET_DB_PATH": market_path, "PAPER_INITIAL_CASH": "1000"},
                        clear=True,
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = main(
                        [
                            "backtest-portfolio-ma",
                            "005930",
                            "000660",
                            "--count",
                            "5",
                            "--short-window",
                            "2",
                            "--long-window",
                            "3",
                            "--max-open-positions",
                            "1",
                            "--format",
                            output_format,
                        ]
                    )
                self.assertEqual(exit_code, 0)
                outputs.append(output.getvalue())

        payload = json.loads(outputs[0])
        self.assertEqual(payload["symbols"], ["000660", "005930"])
        self.assertEqual(len(payload["positions"]), 2)
        self.assertEqual(len(payload["trades"]), 1)
        self.assertEqual(payload["max_open_position_rejections"], 1)
        csv_lines = outputs[1].splitlines()
        self.assertIn("portfolio_final_equity", csv_lines[0])
        self.assertIn("symbol_unrealized_pnl", csv_lines[0])
        self.assertIn("symbol_insufficient_cash_buys", csv_lines[0])
        self.assertIn("symbol_max_open_position_rejections", csv_lines[0])
        self.assertEqual(len(csv_lines), 3)

    def test_portfolio_backtest_rejects_duplicate_symbols(self) -> None:
        output = io.StringIO()
        with redirect_stderr(output):
            exit_code = main(
                ["backtest-portfolio-ma", "005930", "005930", "--count", "5"]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("symbols must not contain duplicates", output.getvalue())

    def test_walk_forward_supports_csv_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_path = str(Path(directory) / "market.db")
            repository = SqliteMarketRepository(market_path)
            started_at = datetime(2026, 1, 1, tzinfo=UTC)
            repository.upsert_candles(
                [
                    Candle(
                        symbol="005930",
                        interval="1d",
                        timestamp=started_at.replace(day=index + 1),
                        open_price=Decimal(100 + index),
                        high_price=Decimal(100 + index),
                        low_price=Decimal(100 + index),
                        close_price=Decimal(100 + index),
                        volume=Decimal(1000),
                        currency="KRW",
                    )
                    for index in range(20)
                ]
            )
            repository.close()
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"MARKET_DB_PATH": market_path}, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "walk-forward-ma",
                        "005930",
                        "--count",
                        "20",
                        "--short-windows",
                        "2",
                        "3",
                        "--long-windows",
                        "4",
                        "--train-ratio",
                        "0.6",
                        "--format",
                        "csv",
                    ]
                )

        lines = output.getvalue().splitlines()
        self.assertEqual(exit_code, 0)
        self.assertIn("validation_excess_return_rate", lines[0])
        self.assertEqual(len(lines), 3)

    def test_backtests_stored_candles_without_writing_paper_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_path = str(Path(directory) / "market.db")
            repository = SqliteMarketRepository(market_path)
            started_at = datetime(2026, 1, 1, tzinfo=UTC)
            repository.upsert_candles(
                [
                    Candle(
                        symbol="005930",
                        interval="1d",
                        timestamp=started_at.replace(day=index + 1),
                        open_price=Decimal(close),
                        high_price=Decimal(close),
                        low_price=Decimal(close),
                        close_price=Decimal(close),
                        volume=Decimal(1000),
                        currency="KRW",
                    )
                    for index, close in enumerate(
                        [10000, 10000, 10000, 12000, 12000, 12000, 8000]
                    )
                ]
            )
            repository.close()
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {"MARKET_DB_PATH": market_path, "PAPER_INITIAL_CASH": "1000000"},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "backtest-ma",
                        "005930",
                        "--count",
                        "7",
                        "--quantity",
                        "10",
                        "--short-window",
                        "2",
                        "--long-window",
                        "3",
                        "--slippage-bps",
                        "10",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["slippage_rate"], "0.001")
        self.assertIn("buy_hold_return_rate", payload)
        self.assertIn("excess_return_rate", payload)

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

    def test_intraday_backfill_defaults_to_top_thirty(self) -> None:
        args = build_parser().parse_args(
            ["backfill-intraday-samples", "--as-of", "2026-08-20"]
        )

        self.assertEqual(args.command, "backfill-intraday-samples")
        self.assertEqual(args.as_of.isoformat(), "2026-08-20")
        self.assertEqual(args.max_eligible_rank, 30)

    def test_historical_intraday_backfill_starts_at_session_close(self) -> None:
        cutoff = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)

        cursor = _intraday_backfill_start_cursor(
            session_day=cutoff.date(),
            today=datetime(2026, 8, 21, tzinfo=UTC).date(),
            cutoff=cutoff,
        )

        self.assertEqual(cursor, cutoff.isoformat())

    def test_same_day_intraday_backfill_starts_from_latest_page(self) -> None:
        cutoff = datetime(2026, 8, 21, 6, 30, tzinfo=UTC)

        cursor = _intraday_backfill_start_cursor(
            session_day=cutoff.date(),
            today=cutoff.date(),
            cutoff=cutoff,
        )

        self.assertIsNone(cursor)

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

    def test_serve_paper_mcp_is_internal_read_only(self) -> None:
        args = build_parser().parse_args(
            ["serve-paper-mcp", "--host", "127.0.0.1", "--port", "8090"]
        )

        self.assertEqual(args.command, "serve-paper-mcp")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8090)
        self.assertFalse(hasattr(args, "live"))

    def test_queries_persisted_risk_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            ledger = PaperLedger(database_path)
            PaperTradingService(
                ledger=ledger,
                risk_manager=RiskManager(RiskLimits()),
            ).submit(
                TradeSignal(
                    signal_id="cli-risk-buy",
                    symbol="005930",
                    side=Side.BUY,
                    reference_price=Decimal(70000),
                    quantity=Decimal(1),
                    reason="audit fixture",
                ),
                now=datetime(2026, 8, 12, tzinfo=UTC),
            )
            ledger.close()
            output = io.StringIO()

            with (
                patch.dict(
                    "os.environ",
                    {"PAPER_DB_PATH": database_path},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    ["risk-decisions", "--symbol", "005930", "--limit", "10"]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["decisions"][0]["approved"])

    def test_queries_automation_run_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            ledger = PaperLedger(database_path)
            ledger.record_automation_run(
                run_type="market_scan",
                status="succeeded",
                stage="completed",
                started_at=datetime(2026, 8, 12, 8, 30, tzinfo=UTC),
                finished_at=datetime(2026, 8, 12, 8, 30, 1, tzinfo=UTC),
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            )
            ledger.close()
            output = io.StringIO()

            with (
                patch.dict(
                    "os.environ",
                    {"PAPER_DB_PATH": database_path},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    ["automation-runs", "--type", "market_scan", "--limit", "10"]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["runs"][0]["totalTokens"], 120)

    def test_queries_n8n_flow_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "paper.db")
            ledger = PaperLedger(database_path)
            ledger.record_automation_run(
                run_type="n8n_flow",
                status="skipped",
                stage="telegram-report",
                started_at=datetime(2026, 8, 13, 7, 0, tzinfo=UTC),
                finished_at=datetime(2026, 8, 13, 7, 0, 1, tzinfo=UTC),
                details={"executionId": "77"},
            )
            ledger.close()
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"PAPER_DB_PATH": database_path}, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(
                    ["automation-runs", "--type", "n8n_flow", "--status", "skipped"]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["runs"][0]["details"]["executionId"], "77")


if __name__ == "__main__":
    unittest.main()
