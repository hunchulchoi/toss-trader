from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from toss_trader.calendar import MarketSession
from toss_trader.cli import (
    _momentum_observed_symbols,
    _momentum_ranked_symbols,
    _record_momentum_shadow_once,
)
from toss_trader.models import Candle
from toss_trader.momentum_shadow import (
    evaluate_momentum_shadow,
    evaluate_momentum_shadow_outcome,
    ranking_symbols,
)

SEOUL = ZoneInfo("Asia/Seoul")
DAY = date(2026, 8, 24)
OPEN = datetime(2026, 8, 24, 9, 0, tzinfo=SEOUL)
SESSION = MarketSession("KR", DAY, True, OPEN, OPEN.replace(hour=15, minute=30))


class FakeRepository:
    def __init__(self, rows: dict[str, list[Candle]]) -> None:
        self.rows = rows

    def latest_candles(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[Candle]:
        if interval != "1m":
            return []
        return list(self.rows.get(symbol, ())[-limit:])


def series(symbol: str, *, volume: Decimal = Decimal(100), spike: bool = False):
    closes = []
    for minute in range(1, 62):
        if minute <= 14:
            close = Decimal(100) + Decimal(minute) * Decimal("0.285714")
        elif minute == 15:
            close = Decimal(103)
        elif minute == 16:
            close = Decimal(102)
        elif minute == 17:
            close = Decimal(103)
        else:
            close = Decimal(104)
        if spike and minute == 5:
            close = Decimal(109)
        opening = Decimal(100) if minute == 1 else closes[-1]
        closes.append(close)
        yield Candle(
            symbol=symbol,
            interval="1m",
            timestamp=OPEN + timedelta(minutes=minute),
            open_price=opening,
            high_price=max(opening, close) + Decimal("0.1"),
            low_price=(Decimal("101.5") if minute == 16 else min(opening, close)),
            close_price=close,
            volume=volume,
            currency="KRW",
        )


def proxy(symbol: str, *, aligned: bool = True):
    rows = []
    for minute in range(1, 61):
        close = Decimal(100) + (Decimal(minute) if aligned else -Decimal(minute)) / 100
        opening = Decimal(100) if minute == 1 else rows[-1].close_price
        rows.append(
            Candle(
                symbol=symbol,
                interval="1m",
                timestamp=OPEN + timedelta(minutes=minute),
                open_price=opening,
                high_price=max(opening, close),
                low_price=min(opening, close),
                close_price=close,
                volume=Decimal(1000),
                currency="KRW",
            )
        )
    return rows


class MomentumShadowTest(unittest.TestCase):
    def test_filters_ranking_to_allowed_unique_top_thirty(self) -> None:
        payload = {
            "rankings": [
                {"symbol": "AAA"},
                {"symbol": "ETF"},
                {"symbol": "AAA"},
                {"symbol": "BBB"},
            ]
        }

        self.assertEqual(
            ranking_symbols(
                payload, allowed_symbols=frozenset({"AAA", "BBB"}), limit=30
            ),
            ("AAA", "BBB"),
        )

    def test_selects_three_bar_reclaim_with_aligned_market(self) -> None:
        repository = FakeRepository(
            {
                "AAA": list(series("AAA", volume=Decimal(300))),
                "BBB": list(series("BBB", volume=Decimal(100))),
                "069500": proxy("069500"),
                "229200": proxy("229200"),
            }
        )

        result = evaluate_momentum_shadow(
            repository,
            symbols=("BBB", "AAA"),
            market_by_symbol={"AAA": "KOSPI", "BBB": "KOSPI"},
            session=SESSION,
            observed_at=OPEN.replace(hour=10, minute=1),
        )

        self.assertEqual(result["status"], "evaluated")
        self.assertTrue(result["shadowOnly"])
        self.assertFalse(result["strategyInput"])
        self.assertEqual(result["candidateCount"], 2)
        self.assertEqual([row["symbol"] for row in result["selected"]], ["AAA", "BBB"])
        self.assertEqual(result["selected"][0]["rewardMultiple"], "1.5")
        self.assertEqual(result["selected"][0]["targetPrice"], "107.75")
        self.assertEqual(
            result["selected"][0]["entryAt"],
            OPEN.replace(hour=10, minute=1).isoformat(),
        )

    def test_opening_spike_and_wrong_market_direction_are_rejected(self) -> None:
        repository = FakeRepository(
            {
                "SPIKE": list(series("SPIKE", spike=True)),
                "DOWN": list(series("DOWN")),
                "069500": proxy("069500", aligned=False),
                "229200": proxy("229200"),
            }
        )

        result = evaluate_momentum_shadow(
            repository,
            symbols=("SPIKE", "DOWN"),
            market_by_symbol={"SPIKE": "KOSPI", "DOWN": "KOSPI"},
            session=SESSION,
            observed_at=OPEN.replace(hour=10, minute=1),
        )

        self.assertEqual(result["selected"], [])
        self.assertEqual(result["reasons"]["opening-spike"], 1)
        self.assertEqual(result["reasons"]["market-not-aligned"], 1)

    def test_waits_until_selection_time_without_reading_future(self) -> None:
        repository = FakeRepository({})

        result = evaluate_momentum_shadow(
            repository,
            symbols=("AAA",),
            market_by_symbol={"AAA": "KOSPI"},
            session=SESSION,
            observed_at=OPEN.replace(hour=9, minute=59),
        )

        self.assertEqual(result["status"], "waiting")

    def test_all_missing_candles_are_incomplete_not_normal_zero(self) -> None:
        result = evaluate_momentum_shadow(
            FakeRepository({"069500": proxy("069500"), "229200": proxy("229200")}),
            symbols=("AAA",),
            market_by_symbol={"AAA": "KOSPI"},
            session=SESSION,
            observed_at=OPEN.replace(hour=10, minute=1),
        )

        self.assertEqual(result["status"], "incomplete-data")
        self.assertEqual(result["reasons"], {"incomplete-1m": 1})

    def test_top_gainers_are_shadow_only_allowed_pool_sample(self) -> None:
        class Client:
            def rankings(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "rankings": [
                        {"symbol": "ETF"},
                        {"symbol": "AAA"},
                        {"symbol": "BBB"},
                    ]
                }

        client = Client()

        result = _momentum_ranked_symbols(
            client, allowed_symbols=frozenset({"AAA", "BBB"})
        )

        self.assertEqual(result, ("AAA", "BBB"))
        self.assertEqual(client.kwargs["ranking_type"], "TOP_GAINERS")
        self.assertEqual(client.kwargs["count"], 100)

    def test_shadow_audit_is_idempotent_per_session_and_rule(self) -> None:
        class Ledger:
            def __init__(self):
                self.rows = []

            def recent_automation_runs(self, **_kwargs):
                return list(self.rows)

            def record_automation_run(self, **kwargs):
                run_id = f"run-{len(self.rows) + 1}"
                self.rows.append(
                    {
                        "runId": run_id,
                        "details": dict(kwargs["details"]),
                    }
                )
                return run_id

        ledger = Ledger()
        first = {"sessionDate": DAY.isoformat(), "ruleVersion": "momentum-shadow-v2"}
        second = dict(first)

        first_id = _record_momentum_shadow_once(
            ledger, payload=first, observed_at=OPEN.replace(hour=10)
        )
        second_id = _record_momentum_shadow_once(
            ledger, payload=second, observed_at=OPEN.replace(hour=10, minute=5)
        )

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(ledger.rows), 1)
        self.assertFalse(first["cacheHit"])
        self.assertTrue(second["cacheHit"])

    def test_retry_collects_only_symbols_observed_by_ten_oclock(self) -> None:
        repository = FakeRepository(
            {
                "SEEN": list(series("SEEN")),
                "FUTURE": [
                    Candle(
                        symbol="FUTURE",
                        interval="1m",
                        timestamp=OPEN.replace(hour=10, minute=5),
                        open_price=Decimal(100),
                        high_price=Decimal(100),
                        low_price=Decimal(100),
                        close_price=Decimal(100),
                        volume=Decimal(1),
                        currency="KRW",
                    )
                ],
            }
        )

        result = _momentum_observed_symbols(
            repository,
            symbols=("SEEN", "FUTURE", "MISSING"),
            session=SESSION,
            observed_at=OPEN.replace(hour=10, minute=5),
        )

        self.assertEqual(result, ("SEEN",))

    def test_shadow_outcome_uses_target_and_conservative_same_bar_stop(self) -> None:
        plan = {
            "symbol": "AAA",
            "entryAt": OPEN.replace(hour=10).isoformat(),
            "entryPrice": "100",
            "stopPrice": "98",
            "targetPrice": "103",
        }
        target_rows = [
            Candle(
                symbol="AAA",
                interval="1m",
                timestamp=OPEN.replace(hour=10),
                open_price=Decimal(100),
                high_price=Decimal(103),
                low_price=Decimal(99),
                close_price=Decimal(102),
                volume=Decimal(1),
                currency="KRW",
            )
        ]
        ambiguous_rows = [
            Candle(
                symbol="AAA",
                interval="1m",
                timestamp=OPEN.replace(hour=10),
                open_price=Decimal(100),
                high_price=Decimal(104),
                low_price=Decimal(97),
                close_price=Decimal(102),
                volume=Decimal(1),
                currency="KRW",
            )
        ]

        target = evaluate_momentum_shadow_outcome(plan, target_rows)
        ambiguous = evaluate_momentum_shadow_outcome(plan, ambiguous_rows)

        self.assertEqual(target["status"], "target")
        self.assertEqual(target["returnRate"], "0.03")
        self.assertEqual(target["rMultiple"], "1.5")
        self.assertEqual(ambiguous["status"], "stopped")
        self.assertEqual(ambiguous["returnRate"], "-0.02")


if __name__ == "__main__":
    unittest.main()
