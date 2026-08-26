from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from toss_trader.cycle import HERMES_EXPERIMENTAL_SIZING_POLICY
from toss_trader.models import Candle
from toss_trader.setup_screening import (
    DEFAULT_POSITION_SIZING_POLICY,
    SetupDecision,
    SetupType,
    ValuationTier,
    hermes_experimental_can_arm,
    position_size_reference,
)
from toss_trader.v2_engine import (
    ADVERSE_SLIPPAGE,
    DailySetupCandidate,
    arm_candidate,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "052690_2026-08-26_gap_up_chase.json"
)
SEOUL = ZoneInfo("Asia/Seoul")


def _candle(row: list[str]) -> Candle:
    return Candle(
        symbol="052690",
        interval="1m",
        timestamp=datetime.fromisoformat(row[0]),
        open_price=Decimal(row[1]),
        high_price=Decimal(row[2]),
        low_price=Decimal(row[3]),
        close_price=Decimal(row[4]),
        volume=Decimal(row[5]),
        currency="KRW",
    )


class GapChaseShadowFixtureTest(unittest.TestCase):
    def test_kepco_engineering_gap_rejection_and_path_evidence(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text())
        source = fixture["candidate"]
        expected = fixture["expected"]
        original = SetupDecision(
            symbol=source["symbol"],
            approved=source["originalApproved"],
            setups=(),
            violations=tuple(source["referenceViolations"]),
            missing_checks=tuple(source["missingChecks"]),
            rsi14=Decimal(source["rsi14"]),
            ma50=Decimal(source["ma50"]),
            ma200=Decimal(source["ma200"]),
            ma50_distance=Decimal(source["ma50Distance"]),
            flow_stars=source["flowStars"],
            flow_summary=None,
            valuation_tier=ValuationTier(source["valuationTier"]),
            confidence_multiplier=Decimal(source["confidenceMultiplier"]),
            proposed_confidence_multiplier=Decimal(
                source["proposedConfidenceMultiplier"]
            ),
        )
        self.assertTrue(hermes_experimental_can_arm(original))
        experimental = replace(
            original,
            approved=True,
            setups=(SetupType.HERMES_EXPERIMENTAL,),
        )
        candidate = DailySetupCandidate(
            symbol=source["symbol"],
            signal_session=date.fromisoformat(source["signalSession"]),
            close_price=Decimal(source["signalClose"]),
            setup_low=Decimal(source["setupLow"]),
            ma50=Decimal(source["ma50"]),
            atr14=Decimal(source["atr14"]),
            decision=experimental,
        )
        bars = [_candle(row) for row in fixture["minuteBars"]]
        decision = arm_candidate(
            candidate,
            first_completed_bar=bars[0],
            execution_bar=bars[4],
            session_open_at=datetime(2026, 8, 26, 9, 0, tzinfo=SEOUL),
            equity=Decimal(1_000_000),
            available_cash=Decimal(1_000_000),
            sizing_policy=HERMES_EXPERIMENTAL_SIZING_POLICY,
        )

        self.assertFalse(decision.armed)
        self.assertEqual(decision.reason, expected["authoritativeReason"])
        self.assertIsNone(decision.plan)
        self.assertEqual(
            bars[0].open_price / candidate.close_price - Decimal(1),
            Decimal(expected["gapRate"]),
        )

        hermes_sizing = position_size_reference(
            symbol=candidate.symbol,
            equity=Decimal(1_000_000),
            reference_price=bars[4].close_price,
            stop_price=candidate.setup_low,
            atr=candidate.atr14,
            available_cash=Decimal(1_000_000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
            policy=HERMES_EXPERIMENTAL_SIZING_POLICY,
            slippage=ADVERSE_SLIPPAGE,
        )
        rule_sizing = position_size_reference(
            symbol=candidate.symbol,
            equity=Decimal(1_000_000),
            reference_price=bars[4].close_price,
            stop_price=candidate.setup_low,
            atr=candidate.atr14,
            available_cash=Decimal(1_000_000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
            policy=DEFAULT_POSITION_SIZING_POLICY,
            slippage=ADVERSE_SLIPPAGE,
        )
        self.assertEqual(hermes_sizing.quantity, Decimal(1))
        self.assertEqual(rule_sizing.quantity, Decimal(0))
        self.assertEqual(
            hermes_sizing.atr_stop_floor, Decimal(expected["atrStopFloor"])
        )
        self.assertEqual(
            bars[4].close_price - hermes_sizing.effective_stop_distance,
            Decimal(expected["counterfactualStop"]),
        )

        entry = Decimal(expected["entryPriceWith5bp"])
        after_reference = bars[5:]
        self.assertEqual(
            min(bar.low_price for bar in after_reference) / entry - Decimal(1),
            Decimal(expected["maeThrough0930"]),
        )
        self.assertEqual(
            max(bar.high_price for bar in after_reference) / entry - Decimal(1),
            Decimal(expected["mfeThrough0930"]),
        )
        self.assertEqual(
            bars[-1].close_price / entry - Decimal(1),
            Decimal(expected["returnAt0930"]),
        )
        self.assertEqual(
            min(bar.low_price for bar in bars[:3]) / bars[0].high_price - Decimal(1),
            Decimal(expected["openingHighTo0903Low"]),
        )

        post = [_candle(row) for row in fixture["postWindowCheckpoints"]]
        self.assertEqual(
            post[0].high_price / entry - Decimal(1),
            Decimal(expected["mfeThrough1051"]),
        )
        self.assertEqual(
            post[-1].close_price / entry - Decimal(1),
            Decimal(expected["returnAt1051"]),
        )
        self.assertEqual(
            post[-1].close_price / post[0].high_price - Decimal(1),
            Decimal(expected["peakTo1051Drawdown"]),
        )


if __name__ == "__main__":
    unittest.main()
