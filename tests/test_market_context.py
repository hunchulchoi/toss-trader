from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from toss_trader.calendar import MarketSession
from toss_trader.market_context import build_market_context
from toss_trader.models import Candle


SEOUL = ZoneInfo("Asia/Seoul")


def _candle(
    symbol: str,
    interval: str,
    stamp: datetime,
    close: str,
    *,
    open_price: str | None = None,
) -> Candle:
    price = Decimal(close)
    opening = Decimal(open_price or close)
    return Candle(
        symbol=symbol,
        interval=interval,
        timestamp=stamp,
        open_price=opening,
        high_price=max(opening, price),
        low_price=min(opening, price),
        close_price=price,
        volume=Decimal(1),
        currency="KRW",
    )


class FakeCandles:
    def __init__(self, rows: dict[tuple[str, str], list[Candle]]) -> None:
        self.rows = rows

    def latest_candles(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[Candle]:
        return list(self.rows.get((symbol, interval), [])[-limit:])


class MarketContextTest(unittest.TestCase):
    def test_compares_session_move_against_benchmark_and_prev_close(self) -> None:
        session = MarketSession(
            country="KR",
            business_date=datetime(2026, 8, 24, tzinfo=SEOUL).date(),
            is_business_day=True,
            market_open_at=datetime(2026, 8, 24, 9, 0, tzinfo=SEOUL),
            market_close_at=datetime(2026, 8, 24, 15, 30, tzinfo=SEOUL),
        )
        now = datetime(2026, 8, 24, 11, 50, tzinfo=SEOUL)
        repository = FakeCandles(
            {
                ("069500", "1m"): [
                    _candle(
                        "069500",
                        "1m",
                        datetime(2026, 8, 24, 9, 1, tzinfo=SEOUL),
                        "40000",
                        open_price="40000",
                    ),
                    _candle(
                        "069500",
                        "1m",
                        datetime(2026, 8, 24, 11, 45, tzinfo=SEOUL),
                        "40400",
                    ),
                ],
                ("069500", "1d"): [
                    _candle(
                        "069500",
                        "1d",
                        datetime(2026, 8, 21, 15, 30, tzinfo=SEOUL),
                        "39800",
                    )
                ],
                ("278470", "1m"): [
                    _candle(
                        "278470",
                        "1m",
                        datetime(2026, 8, 24, 9, 1, tzinfo=SEOUL),
                        "10000",
                        open_price="10000",
                    ),
                    _candle(
                        "278470",
                        "1m",
                        datetime(2026, 8, 24, 11, 45, tzinfo=SEOUL),
                        "9700",
                    ),
                ],
                ("278470", "1d"): [
                    _candle(
                        "278470",
                        "1d",
                        datetime(2026, 8, 21, 15, 30, tzinfo=SEOUL),
                        "10100",
                    )
                ],
            }
        )

        payload = build_market_context(
            repository,
            symbols=("278470",),
            benchmark_symbols=("069500",),
            session=session,
            now=now,
            names={"069500": "KODEX 200", "278470": "에이피알"},
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["benchmarks"][0]["vsOpen"], "0.0100")
        stock = payload["symbols"][0]
        self.assertEqual(stock["symbol"], "278470")
        self.assertEqual(stock["vsOpen"], "-0.0300")
        self.assertEqual(stock["vsPrevClose"], "-0.0396")
        self.assertEqual(stock["coverage"], "session-1m")

    def test_closed_session_does_not_invent_prices(self) -> None:
        session = MarketSession(
            country="KR",
            business_date=datetime(2026, 8, 23, tzinfo=SEOUL).date(),
            is_business_day=False,
            market_open_at=None,
            market_close_at=None,
        )
        payload = build_market_context(
            FakeCandles({}),
            symbols=("005930",),
            benchmark_symbols=("069500",),
            session=session,
            now=datetime(2026, 8, 23, 11, 50, tzinfo=SEOUL),
        )
        self.assertEqual(payload["status"], "closed")
        self.assertNotIn("symbols", payload)
