from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from toss_trader.calendar import MarketSession
from toss_trader.models import Candle
from toss_trader.setup_parameter_shadow import evaluate_setup_parameter_shadow

SEOUL = ZoneInfo("Asia/Seoul")
DAY = date(2026, 8, 27)
SIGNAL_DAY = date(2026, 8, 26)
OPEN = datetime(2026, 8, 27, 9, 0, tzinfo=SEOUL)
SESSION = MarketSession("KR", DAY, True, OPEN, OPEN.replace(hour=15, minute=30))


class Repository:
    def __init__(self, rows: dict[tuple[str, str], list[Candle]]) -> None:
        self.rows = rows

    def latest_candles(self, symbol: str, interval: str, *, limit: int):
        return list(self.rows.get((symbol, interval), ())[-limit:])


def daily(symbol: str, closes: list[int], *, last_open: int | None = None):
    started = datetime(2025, 11, 1, 15, 30, tzinfo=SEOUL)
    rows = []
    for index, close in enumerate(closes):
        opening = close if index != len(closes) - 1 or last_open is None else last_open
        rows.append(
            Candle(
                symbol=symbol,
                interval="1d",
                timestamp=started + timedelta(days=index),
                open_price=Decimal(opening),
                high_price=Decimal(max(opening, close) + 2),
                low_price=Decimal(min(opening, close) - 2),
                close_price=Decimal(close),
                volume=Decimal(1000),
                currency="KRW",
            )
        )
    rows[-1] = Candle(
        symbol=symbol,
        interval="1d",
        timestamp=datetime.combine(
            SIGNAL_DAY, datetime.min.time(), tzinfo=SEOUL
        ).replace(hour=15, minute=30),
        open_price=rows[-1].open_price,
        high_price=rows[-1].high_price,
        low_price=rows[-1].low_price,
        close_price=rows[-1].close_price,
        volume=rows[-1].volume,
        currency="KRW",
    )
    return rows


def minutes(symbol: str, price: Decimal, *, through: int = 30):
    return [
        Candle(
            symbol=symbol,
            interval="1m",
            timestamp=OPEN + timedelta(minutes=minute),
            open_price=price,
            high_price=price + 1,
            low_price=price - 1,
            close_price=price,
            volume=Decimal(100),
            currency="KRW",
        )
        for minute in range(1, through + 1)
    ]


class SetupParameterShadowTest(unittest.TestCase):
    def test_records_current_tight_gap_and_sizing_variants_without_strategy_input(self) -> None:
        symbol = "005930"
        history = daily(symbol, [*range(100, 250), *([249, 251] * 25)])
        opening = history[-1].close_price + Decimal(1)
        repository = Repository(
            {
                (symbol, "1d"): history,
                (symbol, "1m"): minutes(symbol, opening),
            }
        )

        result = evaluate_setup_parameter_shadow(
            repository,
            symbols=(symbol,),
            session=SESSION,
            signal_session=SIGNAL_DAY,
            observed_at=OPEN.replace(hour=10),
        )

        self.assertEqual(result["status"], "evaluated")
        self.assertFalse(result["strategyInput"])
        self.assertTrue(result["shadowOnly"])
        self.assertFalse(result["strictPITApproved"])
        self.assertEqual(result["dataQuality"]["openingCoverageRate"], "1")
        self.assertEqual(result["sizingAssumptions"]["existingOpenHeat"], "0")
        row = result["rows"][0]
        self.assertTrue(row["currentPullback4Pct"])
        self.assertTrue(row["tightPullback2Pct"])
        self.assertTrue(row["gap2PctPass"])
        self.assertTrue(row["gap3PctPass"])
        self.assertEqual(row["firstValidAt"], OPEN.replace(hour=9, minute=5).isoformat())
        self.assertEqual(len(row["dailyEvidenceHash"]), 64)
        self.assertEqual(len(row["openingEvidenceHash"]), 64)
        self.assertEqual(
            set(row["quantities"]),
            {
                "ruleRisk0_5Atr1_5",
                "risk1Atr1_5",
                "risk0_5Atr1",
                "risk1Atr1",
            },
        )

    def test_alternate_oversold_confirmation_is_separate_from_authoritative_rule(self) -> None:
        symbol = "000660"
        closes = [200] * 185 + list(range(199, 185, -1)) + [188]
        history = daily(symbol, closes, last_open=185)
        previous = history[-2]
        history[-2] = Candle(
            symbol=previous.symbol,
            interval=previous.interval,
            timestamp=previous.timestamp,
            open_price=previous.open_price,
            high_price=Decimal(190),
            low_price=previous.low_price,
            close_price=previous.close_price,
            volume=previous.volume,
            currency=previous.currency,
        )
        repository = Repository(
            {
                (symbol, "1d"): history,
                (symbol, "1m"): minutes(symbol, Decimal(190)),
            }
        )

        result = evaluate_setup_parameter_shadow(
            repository,
            symbols=(symbol,),
            session=SESSION,
            signal_session=SIGNAL_DAY,
            observed_at=OPEN.replace(hour=10),
        )

        row = result["rows"][0]
        self.assertFalse(row["currentOversoldRsi35PreviousHigh"])
        self.assertTrue(row["alternateOversoldRsi40PreviousClose"])
        self.assertFalse(row["currentPriceSetup"])
        self.assertIn("quantities", row)
        self.assertEqual(
            result["variants"]["alternateOversoldGap3ValidStopBy0930"], 1
        )

    def test_marks_partial_data_and_does_not_use_post_cutoff_bar(self) -> None:
        symbol = "005930"
        history = daily(symbol, [*range(100, 250), *([249, 251] * 25)])
        rows = [
            row
            for row in minutes(symbol, Decimal(252))
            if row.timestamp != OPEN.replace(hour=9, minute=15)
        ]
        rows.append(
            Candle(
                symbol=symbol,
                interval="1m",
                timestamp=OPEN.replace(hour=10, minute=5),
                open_price=Decimal(252),
                high_price=Decimal(253),
                low_price=Decimal(251),
                close_price=Decimal(252),
                volume=Decimal(100),
                currency="KRW",
            )
        )
        repository = Repository(
            {(symbol, "1d"): history, (symbol, "1m"): rows}
        )

        result = evaluate_setup_parameter_shadow(
            repository,
            symbols=(symbol,),
            session=SESSION,
            signal_session=SIGNAL_DAY,
            observed_at=OPEN.replace(hour=10),
        )

        self.assertEqual(result["status"], "partial-data")
        self.assertEqual(result["dataQuality"]["openingComplete"], 0)
        self.assertEqual(
            result["dataQuality"]["reasons"], {"incomplete-opening-1m": 1}
        )

    def test_one_invalid_sizing_reference_does_not_discard_the_daily_study(self) -> None:
        symbol = "005930"
        history = daily(symbol, [*range(100, 250), *([249, 251] * 25)])
        for index in range(len(history) - 14, len(history)):
            row = history[index]
            history[index] = Candle(
                symbol=row.symbol,
                interval=row.interval,
                timestamp=row.timestamp,
                open_price=row.open_price,
                high_price=Decimal(1000),
                low_price=Decimal(1),
                close_price=row.close_price,
                volume=row.volume,
                currency=row.currency,
            )
        repository = Repository(
            {
                (symbol, "1d"): history,
                (symbol, "1m"): minutes(symbol, Decimal(252)),
            }
        )

        result = evaluate_setup_parameter_shadow(
            repository,
            symbols=(symbol,),
            session=SESSION,
            signal_session=SIGNAL_DAY,
            observed_at=OPEN.replace(hour=10),
        )

        self.assertEqual(result["status"], "evaluated")
        row = result["rows"][0]
        self.assertEqual(row["quantities"], {})
        self.assertEqual(len(row["sizingErrors"]), 4)
        self.assertEqual(result["variants"]["invalidSizingReference"], 1)

    def test_waits_for_complete_thirty_minute_observation_window(self) -> None:
        result = evaluate_setup_parameter_shadow(
            Repository({}),
            symbols=("005930",),
            session=SESSION,
            signal_session=SIGNAL_DAY,
            observed_at=OPEN.replace(hour=9, minute=29),
        )

        self.assertEqual(result["status"], "waiting")

    def test_missing_daily_is_partial_instead_of_a_normal_zero_setup(self) -> None:
        result = evaluate_setup_parameter_shadow(
            Repository({("005930", "1m"): minutes("005930", Decimal(100))}),
            symbols=("005930",),
            session=SESSION,
            signal_session=SIGNAL_DAY,
            observed_at=OPEN.replace(hour=10),
        )

        self.assertEqual(result["status"], "partial-data")
        self.assertEqual(result["dataQuality"]["reasons"], {"missing-daily": 1})


if __name__ == "__main__":
    unittest.main()
