import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Candle
from toss_trader.setup_screening import (
    FlowSnapshot,
    SetupContext,
    SetupType,
    ValuationEvidence,
    ValuationTier,
    evaluate_setup,
    position_size_reference,
    valuation_tier,
)


def candles(closes: list[int], *, opens: list[int] | None = None) -> list[Candle]:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    open_prices = opens or closes
    return [
        Candle(
            symbol="005930",
            interval="1d",
            timestamp=started_at + timedelta(days=index),
            open_price=Decimal(open_prices[index]),
            high_price=Decimal(max(open_prices[index], close)),
            low_price=Decimal(min(open_prices[index], close)),
            close_price=Decimal(close),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index, close in enumerate(closes)
    ]


def pullback_candles() -> list[Candle]:
    return candles([*range(100, 250), *([249, 251] * 25)])


class SetupScreeningTest(unittest.TestCase):
    def test_approves_pullback_after_manual_safety_checks(self) -> None:
        result = evaluate_setup(
            pullback_candles(),
            context=SetupContext(event_imminent=False, gap_up_chase=False),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.setups, (SetupType.PULLBACK,))
        self.assertGreaterEqual(result.ma50_distance, Decimal(0))
        self.assertLessEqual(result.ma50_distance, Decimal("0.04"))

    def test_requires_two_setups_in_volatile_market(self) -> None:
        without_flow = evaluate_setup(
            pullback_candles(),
            context=SetupContext(
                volatile_market=True,
                event_imminent=False,
                gap_up_chase=False,
            ),
        )
        with_flow = evaluate_setup(
            pullback_candles(),
            context=SetupContext(
                volatile_market=True,
                flow=FlowSnapshot(
                    foreign_previous=Decimal(-1),
                    foreign_current=Decimal(1),
                    institutional_current=Decimal(1),
                ),
                event_imminent=False,
                gap_up_chase=False,
            ),
        )

        self.assertIn("insufficient-setups", without_flow.violations)
        self.assertTrue(with_flow.approved)
        self.assertEqual(with_flow.flow_stars, 2)

    def test_requires_bullish_confirmation_for_oversold_reversal(self) -> None:
        closes = [200] * 185 + list(range(199, 185, -1)) + [190]
        opens = [*closes[:-1], 185]
        confirmed = candles(closes, opens=opens)
        previous = confirmed[-2]
        confirmed[-2] = Candle(
            symbol=previous.symbol,
            interval=previous.interval,
            timestamp=previous.timestamp,
            open_price=previous.open_price,
            high_price=Decimal(188),
            low_price=previous.low_price,
            close_price=previous.close_price,
            volume=previous.volume,
            currency=previous.currency,
        )

        result = evaluate_setup(
            confirmed,
            context=SetupContext(event_imminent=False, gap_up_chase=False),
        )

        self.assertLessEqual(result.rsi14, Decimal(35))
        self.assertIn(SetupType.OVERSOLD_REVERSAL, result.setups)
        self.assertTrue(result.approved)

    def test_fails_closed_when_manual_safety_checks_are_missing(self) -> None:
        result = evaluate_setup(pullback_candles(), context=SetupContext())

        self.assertFalse(result.approved)
        self.assertEqual(
            result.missing_checks, ("event-calendar", "gap-up-review")
        )

    def test_rejects_known_prohibitions(self) -> None:
        result = evaluate_setup(
            pullback_candles(),
            context=SetupContext(
                stop_price=Decimal(240),
                averaging_down=True,
                event_imminent=True,
                gap_up_chase=True,
            ),
        )

        self.assertFalse(result.approved)
        self.assertIn("stop-line-proximity", result.violations)
        self.assertIn("averaging-down", result.violations)
        self.assertIn("event-imminent", result.violations)
        self.assertIn("gap-up-chase", result.violations)

    def test_rejects_rsi_chase_and_three_percent_drop(self) -> None:
        rising = evaluate_setup(
            candles(list(range(100, 300))),
            context=SetupContext(event_imminent=False, gap_up_chase=False),
        )
        falling_prices = [200] * 199 + [194]
        falling = evaluate_setup(
            candles(falling_prices),
            context=SetupContext(
                flow=FlowSnapshot(
                    foreign_previous=Decimal(-1), foreign_current=Decimal(1)
                ),
                event_imminent=False,
                gap_up_chase=False,
            ),
        )

        self.assertIn("rsi-chase", rising.violations)
        self.assertIn("falling-knife", falling.violations)

    def test_valuation_uses_sector_relative_evidence(self) -> None:
        self.assertEqual(
            valuation_tier(
                ValuationEvidence(
                    forward_per_growth=Decimal("0.25"),
                    sector_per_percentile=Decimal("0.30"),
                )
            ),
            ValuationTier.A,
        )
        self.assertEqual(
            valuation_tier(ValuationEvidence(sector_relative_overvalued=True)),
            ValuationTier.C,
        )
        self.assertEqual(valuation_tier(None), ValuationTier.B)

    def test_position_size_is_reference_then_capped_by_runtime_limits(self) -> None:
        result = position_size_reference(
            stop_loss_rate=Decimal("0.04"),
            max_order_notional=Decimal(300000),
            available_cash=Decimal(1000000),
        )

        self.assertEqual(result.uncapped_notional, Decimal(10000000))
        self.assertEqual(result.executable_notional, Decimal(300000))
        self.assertTrue(result.capped)

    def test_position_size_reference_matches_stop_examples(self) -> None:
        expected = {
            Decimal("0.04"): Decimal(10000000),
            Decimal("0.06"): Decimal("6666666.666666666666666666667"),
            Decimal("0.08"): Decimal(5000000),
        }
        for stop_loss_rate, uncapped in expected.items():
            with self.subTest(stop_loss_rate=stop_loss_rate):
                result = position_size_reference(
                    stop_loss_rate=stop_loss_rate,
                    max_order_notional=Decimal(20000000),
                    available_cash=Decimal(20000000),
                )
                self.assertEqual(result.uncapped_notional, uncapped)


if __name__ == "__main__":
    unittest.main()
