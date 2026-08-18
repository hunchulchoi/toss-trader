import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.cycle_funnel import (
    REVIEW_PURPOSE,
    aggregate_intraday_review,
    format_intraday_review_lines,
    insights_from_runs,
)
from toss_trader.cycle_state import SqliteCycleStateStore


class IntradayReviewTest(unittest.TestCase):
    def test_counts_fills_and_last_reasons_per_symbol(self) -> None:
        review = aggregate_intraday_review(
            [
                {
                    "symbols": [
                        {
                            "symbol": "005930",
                            "reason": "setup-v2-block",
                            "skipReason": "setup-v2:waiting:first-session-bar",
                        }
                    ]
                },
                {
                    "symbols": [
                        {
                            "symbol": "005930",
                            "reason": "v2-idle",
                            "skipReason": None,
                            "fillSide": "BUY",
                        },
                        {
                            "symbol": "000660",
                            "reason": "setup-v2-block",
                            "skipReason": "setup-v2:missing:flow-history",
                        },
                    ]
                },
            ]
        )

        self.assertEqual(review["purpose"], REVIEW_PURPOSE)
        self.assertEqual(review["cycles"], 2)
        self.assertEqual(review["symbols"], 2)
        self.assertEqual(review["buyFills"], 1)
        self.assertEqual(review["sellFills"], 0)
        self.assertEqual(review["lastReasons"]["filled:BUY"], 1)
        self.assertEqual(review["lastReasons"]["setup-v2:missing:flow-history"], 1)
        self.assertIn("005930 BUY 1", " ".join(format_intraday_review_lines(review)))

    def test_empty_insights_report_no_intraday_cycles(self) -> None:
        review = aggregate_intraday_review([])

        self.assertEqual(review["cycles"], 0)
        self.assertEqual(
            format_intraday_review_lines(review),
            ("당일 1m 사이클 기록 없음",),
        )


class InsightsFromRunsTest(unittest.TestCase):
    def test_reads_cycle_insight_in_start_order(self) -> None:
        store = SqliteCycleStateStore(":memory:")
        self.addCleanup(store.close)
        first = store.start_run(
            started_at=datetime(2026, 8, 18, 0, 1, tzinfo=UTC),
            interval="1m",
            symbol_count=1,
        )
        store.finish_run(
            run_id=first,
            finished_at=datetime(2026, 8, 18, 0, 1, 2, tzinfo=UTC),
            status="succeeded",
            signal_count=0,
            fill_count=0,
            failed_count=0,
            consecutive_api_errors=0,
            daily_return_rate=Decimal(0),
            error_message=None,
            cycle_insight=json.dumps(
                {"symbols": [{"symbol": "005930", "reason": "v2-idle"}]}
            ),
        )
        later = store.start_run(
            started_at=datetime(2026, 8, 18, 0, 2, tzinfo=UTC),
            interval="1m",
            symbol_count=1,
        )
        store.finish_run(
            run_id=later,
            finished_at=datetime(2026, 8, 18, 0, 2, 2, tzinfo=UTC),
            status="succeeded",
            signal_count=0,
            fill_count=1,
            failed_count=0,
            consecutive_api_errors=0,
            daily_return_rate=Decimal(0),
            error_message=None,
            cycle_insight=json.dumps(
                {
                    "symbols": [
                        {
                            "symbol": "005930",
                            "reason": None,
                            "fillSide": "BUY",
                        }
                    ]
                }
            ),
        )
        other_day = store.start_run(
            started_at=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
            interval="1m",
            symbol_count=1,
        )
        store.finish_run(
            run_id=other_day,
            finished_at=datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
            status="succeeded",
            signal_count=0,
            fill_count=0,
            failed_count=0,
            consecutive_api_errors=0,
            daily_return_rate=Decimal(0),
            error_message=None,
            cycle_insight=json.dumps(
                {"symbols": [{"symbol": "000660", "reason": "v2-idle"}]}
            ),
        )

        runs = store.list_runs(
            interval="1m",
            started_from=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
            started_to=datetime(2026, 8, 18, 15, 40, tzinfo=UTC),
        )
        review = aggregate_intraday_review(insights_from_runs(runs))

        self.assertEqual(len(runs), 2)
        self.assertEqual(review["buyFills"], 1)
        self.assertEqual(review["symbols"], 1)

    def test_list_runs_skips_running_rows(self) -> None:
        store = SqliteCycleStateStore(":memory:")
        self.addCleanup(store.close)
        store.start_run(
            started_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
            interval="1m",
            symbol_count=1,
        )

        runs = store.list_runs(
            interval="1m",
            started_from=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
            started_to=datetime(2026, 8, 18, 2, 0, tzinfo=UTC),
        )

        self.assertEqual(runs, ())


if __name__ == "__main__":
    unittest.main()
