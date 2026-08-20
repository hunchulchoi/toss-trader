import unittest
from datetime import UTC, date, datetime

from toss_trader.calendar import MarketCalendarService, previous_kr_business_date


class FakeCalendarClient:
    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, date]] = []

    def market_calendar(self, country: str, *, day: date | None = None) -> dict:
        assert day is not None
        self.calls.append((country, day))
        return self.payloads[country]


class MarketCalendarServiceTest(unittest.TestCase):
    def test_parses_kr_regular_market_close(self) -> None:
        client = FakeCalendarClient(
            {
                "KR": {
                    "today": {
                        "date": "2026-08-12",
                        "integrated": {
                            "regularMarket": {
                                "startTime": "2026-08-12T09:00:00+09:00",
                                "endTime": "2026-08-12T15:30:00+09:00",
                            }
                        },
                    }
                }
            }
        )

        session = MarketCalendarService(client).regular_session(
            "KR", now=datetime(2026, 8, 12, 5, 0, tzinfo=UTC)
        )

        self.assertTrue(session.is_business_day)
        self.assertEqual(
            session.market_close_at,
            datetime.fromisoformat("2026-08-12T15:30:00+09:00"),
        )
        self.assertEqual(client.calls, [("KR", date(2026, 8, 12))])

    def test_marks_kr_holiday_closed(self) -> None:
        client = FakeCalendarClient(
            {"KR": {"today": {"date": "2026-08-15", "integrated": None}}}
        )

        session = MarketCalendarService(client).regular_session(
            "KR", now=datetime(2026, 8, 15, 1, 0, tzinfo=UTC)
        )

        self.assertFalse(session.is_business_day)
        self.assertIsNone(session.market_close_at)

    def test_uses_us_local_date_and_us_response_shape(self) -> None:
        client = FakeCalendarClient(
            {
                "US": {
                    "today": {
                        "date": "2026-08-11",
                        "regularMarket": {
                            "startTime": "2026-08-11T22:30:00+09:00",
                            "endTime": "2026-08-12T05:00:00+09:00",
                        },
                    }
                }
            }
        )

        session = MarketCalendarService(client).regular_session(
            "US", now=datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
        )

        self.assertTrue(session.is_business_day)
        self.assertEqual(client.calls, [("US", date(2026, 8, 11))])

    def test_previous_kr_business_date_skips_weekend(self) -> None:
        class DayCalendar:
            def market_calendar(self, country: str, *, day: date | None = None):
                assert day is not None
                if day.weekday() >= 5:
                    return {"today": {"date": day.isoformat(), "integrated": None}}
                return {
                    "today": {
                        "date": day.isoformat(),
                        "integrated": {
                            "regularMarket": {
                                "startTime": f"{day.isoformat()}T09:00:00+09:00",
                                "endTime": f"{day.isoformat()}T15:30:00+09:00",
                            }
                        },
                    }
                }

        monday = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
        self.assertEqual(
            previous_kr_business_date(MarketCalendarService(DayCalendar()), monday),
            date(2026, 8, 14),
        )


if __name__ == "__main__":
    unittest.main()
