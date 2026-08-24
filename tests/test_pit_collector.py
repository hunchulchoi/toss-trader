from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from toss_trader.pit_collector import (
    PitCollectionResult,
    future_kr_sessions,
    run_pit_collection,
    seconds_until_next_run,
    serve_pit_collector,
)

SEOUL = ZoneInfo("Asia/Seoul")


class FakeCalendar:
    def __init__(self, open_days: set[date]) -> None:
        self.open_days = open_days

    def regular_session(self, country: str, *, now: datetime):
        return type(
            "Session",
            (),
            {"is_business_day": now.date() in self.open_days},
        )()


class FakeCollector:
    def __init__(self) -> None:
        self.universe_call = None
        self.event_call = None

    def collect_universe(self, *, start: date, end: date) -> int:
        self.universe_call = (start, end)
        return 42

    def collect_events(
        self,
        *,
        start: date,
        end: date,
        additional_sessions=(),
        checkpoint_through=None,
    ) -> int:
        self.event_call = (
            start,
            end,
            tuple(additional_sessions),
            checkpoint_through,
        )
        return 7


class FakeFlowCollector:
    def __init__(self) -> None:
        self.call = None

    def collect(self, **kwargs) -> int:
        self.call = kwargs
        return 12


class FailedFlowCollector(FakeFlowCollector):
    @property
    def failures(self) -> tuple[str, ...]:
        return ("KIS token request failed: EGW00133",)


class PitCollectorTest(unittest.TestCase):
    def test_future_sessions_skip_weekend_and_holiday(self) -> None:
        open_days = {
            date(2026, 8, 19),
            date(2026, 8, 21),
            date(2026, 8, 24),
        }
        pauses: list[float] = []

        result = future_kr_sessions(
            FakeCalendar(open_days),
            start=date(2026, 8, 19),
            request_pause=pauses.append,
        )

        self.assertEqual(result, tuple(sorted(open_days)))
        self.assertTrue(pauses)

    def test_collection_uses_recent_window_and_future_sessions(self) -> None:
        collector = FakeCollector()
        open_days = {
            date(2026, 8, 18),
            date(2026, 8, 19),
            date(2026, 8, 20),
            date(2026, 8, 21),
        }

        result = run_pit_collection(
            collector,
            FakeCalendar(open_days),
            now=datetime(2026, 8, 18, 18, 30, tzinfo=SEOUL),
        )

        self.assertEqual(collector.universe_call, (date(2026, 8, 4), date(2026, 8, 18)))
        self.assertEqual(
            collector.event_call,
            (
                date(2026, 8, 4),
                date(2026, 8, 18),
                tuple(sorted(open_days)),
                date(2026, 8, 17),
            ),
        )
        self.assertEqual(result.universe_rows, 42)
        self.assertEqual(result.event_rows, 7)
        self.assertEqual(result.flow_status, "UNKNOWN_NO_AUTHORIZED_SOURCE")

    def test_next_run_includes_after_midnight_event_finalization(self) -> None:
        before = datetime(2026, 8, 18, 18, 0, tzinfo=SEOUL)
        after = datetime(2026, 8, 18, 19, 0, tzinfo=SEOUL)
        after_midnight = datetime(2026, 8, 19, 0, 11, tzinfo=SEOUL)

        self.assertEqual(seconds_until_next_run(before), 30 * 60)
        self.assertEqual(seconds_until_next_run(after), 5 * 60 * 60 + 10 * 60)
        self.assertEqual(
            seconds_until_next_run(after_midnight),
            18 * 60 * 60 + 19 * 60,
        )

    def test_kis_flow_waits_until_provider_window(self) -> None:
        flow = FakeFlowCollector()
        result = run_pit_collection(
            FakeCollector(),
            FakeCalendar(
                {
                    date(2026, 8, 18),
                    date(2026, 8, 19),
                    date(2026, 8, 20),
                    date(2026, 8, 21),
                }
            ),
            now=datetime(2026, 8, 18, 11, 45, tzinfo=SEOUL),
            flow_collector=flow,
            flow_symbols=["005930"],
        )

        self.assertIsNone(flow.call)
        self.assertEqual(result.flow_status, "WAITING_FOR_KIS_1540")

    def test_kis_flow_collects_completed_day_after_provider_window(self) -> None:
        flow = FakeFlowCollector()
        result = run_pit_collection(
            FakeCollector(),
            FakeCalendar(
                {
                    date(2026, 8, 18),
                    date(2026, 8, 19),
                    date(2026, 8, 20),
                    date(2026, 8, 21),
                }
            ),
            now=datetime(2026, 8, 18, 18, 30, tzinfo=SEOUL),
            flow_collector=flow,
            flow_symbols=["005930"],
        )

        self.assertEqual(flow.call["completed_through"], date(2026, 8, 18))
        self.assertEqual(result.flow_rows, 12)
        self.assertEqual(result.flow_status, "AVAILABLE_FIRST_OBSERVED")

    def test_kis_flow_skips_closed_market_days(self) -> None:
        flow = FakeFlowCollector()
        result = run_pit_collection(
            FakeCollector(),
            FakeCalendar(
                {
                    date(2026, 8, 14),
                    date(2026, 8, 17),
                    date(2026, 8, 18),
                    date(2026, 8, 19),
                    date(2026, 8, 20),
                }
            ),
            now=datetime(2026, 8, 15, 18, 30, tzinfo=SEOUL),
            flow_collector=flow,
            flow_symbols=["005930"],
        )

        self.assertIsNone(flow.call)
        self.assertEqual(result.flow_rows, 0)
        self.assertEqual(result.flow_status, "SKIPPED_MARKET_CLOSED")
        self.assertEqual(result.flow_failures, ())

    def test_kis_flow_failure_is_exposed_for_alerting(self) -> None:
        flow = FailedFlowCollector()
        result = run_pit_collection(
            FakeCollector(),
            FakeCalendar(
                {
                    date(2026, 8, 18),
                    date(2026, 8, 19),
                    date(2026, 8, 20),
                    date(2026, 8, 21),
                }
            ),
            now=datetime(2026, 8, 18, 18, 30, tzinfo=SEOUL),
            flow_collector=flow,
            flow_symbols=["005930"],
        )

        self.assertEqual(result.flow_status, "PARTIAL_FAILURE")
        self.assertEqual(result.flow_failures, ("KIS token request failed: EGW00133",))

    def test_daemon_reports_kis_flow_failure_once(self) -> None:
        result = PitCollectionResult(
            started_at="2026-08-18T09:30:00+00:00",
            universe_rows=1,
            event_rows=1,
            future_sessions=(),
            flow_failures=("KIS token request failed: EGW00133",),
        )
        reports: list[PitCollectionResult] = []

        serve_pit_collector(lambda _: result, once=True, report_failure=reports.append)

        self.assertEqual(reports, [result])


if __name__ == "__main__":
    unittest.main()
