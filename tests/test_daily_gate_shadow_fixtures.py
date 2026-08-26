from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from toss_trader.cycle import HERMES_EXPERIMENTAL_SIZING_POLICY
from toss_trader.models import Candle, Side, TradeSignal
from toss_trader.official_data import OfficialDataRepository
from toss_trader.paper import toss_trade_costs
from toss_trader.setup_screening import (
    DEFAULT_POSITION_SIZING_POLICY,
    OfficialSetupContextFactory,
    SetupContext,
    SetupDecision,
    SetupType,
    ValuationTier,
    evaluate_price_setups,
    evaluate_setup,
    hermes_experimental_can_arm,
)
from toss_trader.v2_engine import (
    ADVERSE_SLIPPAGE,
    DailySetupCandidate,
    arm_candidate,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEOUL = ZoneInfo("Asia/Seoul")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _samsung_ct_price_candles(source: dict) -> list[Candle]:
    values = source["priceInput"]
    closes = [Decimal(value) for value in values["closes"]]
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            symbol=source["candidate"]["symbol"],
            interval="1d",
            timestamp=started_at + timedelta(days=index),
            open_price=close,
            high_price=close,
            low_price=close,
            close_price=close,
            volume=Decimal(0),
            currency="KRW",
        )
        for index, close in enumerate(closes)
    ]
    candles[-2] = replace(
        candles[-2], high_price=Decimal(values["previousHigh"])
    )
    candles[-1] = replace(
        candles[-1],
        open_price=Decimal(values["latestOpen"]),
        high_price=Decimal(values["latestHigh"]),
        low_price=Decimal(values["latestLow"]),
        close_price=Decimal(values["latestClose"]),
    )
    return candles


def _official_context(source: dict, database_path: str) -> SetupContext:
    coverage = source["eventCoverage"]
    event = source["eventInput"]
    repository = OfficialDataRepository(database_path)
    repository.record_coverage(
        dataset=coverage["dataset"],
        start=date.fromisoformat(coverage["coverageStart"]),
        end=date.fromisoformat(coverage["coverageEnd"]),
        completed_at=coverage["completedAt"],
        source=coverage["source"],
        row_count=coverage["rowCount"],
    )
    repository.upsert_events(
        [
            {
                "symbol": event["symbol"],
                "corp_code": event["corpCode"],
                "receipt_no": event["receiptNo"],
                "receipt_date": event["receiptDate"],
                "report_name": event["reportName"],
                "available_at": event["availableAt"],
                "blocked_through": event["blockedThrough"],
                "is_entry_blocking": event["isEntryBlocking"],
                "is_preannounced": event["isPreannounced"],
                "scheduled_for": event["scheduledFor"],
                "source": event["source"],
                "retrieved_at": event["retrievedAt"],
                "payload_hash": event["payloadHash"],
            }
        ]
    )
    repository.close()
    connection = sqlite3.connect(database_path)
    connection.executemany(
        "INSERT INTO market_flow_pit_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source["candidate"]["symbol"],
                session,
                session_index,
                available_at,
                foreign_net_buy,
                institutional_net_buy,
                trading_value,
                flow_source,
                f"{source['candidate']['symbol']}:{session_index}",
                source["source"]["queriedAt"],
                f"flow-{session_index}",
            )
            for (
                session_index,
                session,
                available_at,
                foreign_net_buy,
                institutional_net_buy,
                trading_value,
                flow_source,
            ) in source["flowInput"]
        ],
    )
    connection.commit()
    connection.close()
    candidate = source["candidate"]
    return OfficialSetupContextFactory(database_path)(
        candidate["symbol"],
        date.fromisoformat(candidate["signalSession"]),
        datetime.fromisoformat(source["source"]["decisionCutoff"]),
        candidate["gapUpChase"],
    )


def _minute_candle(symbol: str, row: list[str]) -> Candle:
    return Candle(
        symbol=symbol,
        interval="1m",
        timestamp=datetime.fromisoformat(row[0]),
        open_price=Decimal(row[1]),
        high_price=Decimal(row[2]),
        low_price=Decimal(row[3]),
        close_price=Decimal(row[4]),
        volume=Decimal(row[5]),
        currency="KRW",
    )


def _approved_decision(symbol: str) -> SetupDecision:
    return SetupDecision(
        symbol=symbol,
        approved=True,
        setups=(SetupType.PULLBACK,),
        violations=(),
        missing_checks=(),
        rsi14=Decimal(50),
        ma50=Decimal(1),
        ma200=Decimal(1),
        ma50_distance=Decimal(0),
        flow_stars=0,
        flow_summary=None,
        valuation_tier=ValuationTier.B,
        confidence_multiplier=Decimal(1),
        proposed_confidence_multiplier=Decimal(1),
    )


class SamsungCtPriceEventShadowFixtureTest(unittest.TestCase):
    def test_separates_price_reference_failure_from_event_hard_veto(self) -> None:
        source = _fixture("028260_2026-08-26_price_event_gate.json")
        candles = _samsung_ct_price_candles(source)
        expected = source["expected"]

        price = evaluate_price_setups(candles)

        self.assertEqual(len(candles), source["priceInput"]["count"])
        self.assertEqual([setup.value for setup in price.setups], expected["priceSetups"])
        self.assertEqual(str(price.rsi14), expected["rsi14"])
        self.assertEqual(str(price.ma50), expected["ma50"])
        self.assertEqual(str(price.ma200), expected["ma200"])
        self.assertEqual(str(price.ma50_distance), expected["ma50Distance"])

        with tempfile.TemporaryDirectory() as directory:
            context = _official_context(source, f"{directory}/market.db")
        self.assertEqual(context.event_imminent, expected["eventImminent"])
        self.assertEqual(len(context.flow_observations), 6)
        self.assertGreater(
            context.decision_at,
            datetime.fromisoformat(source["eventInput"]["blockedThrough"]),
        )
        self.assertEqual(source["eventInput"]["isEntryBlocking"], 0)
        self.assertEqual(source["eventInput"]["isPreannounced"], 1)
        self.assertIsNone(source["eventInput"]["scheduledFor"])

        price_only = evaluate_setup(
            candles,
            context=replace(context, event_imminent=False),
        )
        combined = evaluate_setup(candles, context=context)

        self.assertEqual(
            list(price_only.violations), expected["priceOnlyViolations"]
        )
        self.assertEqual(
            list(combined.violations), expected["combinedViolations"]
        )
        self.assertEqual(
            hermes_experimental_can_arm(price_only),
            expected["hermesCanArmPriceOnly"],
        )
        self.assertEqual(
            hermes_experimental_can_arm(combined),
            expected["hermesCanArmCombined"],
        )


class SamsungFireBelowOneLotShadowFixtureTest(unittest.TestCase):
    def test_reproduces_rule_and_hermes_zero_lot_at_0905(self) -> None:
        source = _fixture("000810_2026-08-26_below_one_lot.json")
        candidate_source = source["candidate"]
        expected = source["expected"]
        symbol = candidate_source["symbol"]
        candidate = DailySetupCandidate(
            symbol=symbol,
            signal_session=date.fromisoformat(candidate_source["signalSession"]),
            close_price=Decimal(candidate_source["signalClose"]),
            setup_low=Decimal(candidate_source["setupLow"]),
            ma50=Decimal(1),
            atr14=Decimal(candidate_source["atr14"]),
            decision=_approved_decision(symbol),
        )
        first_bar = _minute_candle(
            symbol, candidate_source["firstCompletedBar"]
        )
        execution_bar = _minute_candle(symbol, candidate_source["executionBar"])
        opened_at = datetime(2026, 8, 26, 9, 0, tzinfo=SEOUL)

        decisions = {}
        for name, policy in (
            ("rule", DEFAULT_POSITION_SIZING_POLICY),
            ("hermes", HERMES_EXPERIMENTAL_SIZING_POLICY),
        ):
            snapshot = source[f"{name}Snapshot"]
            decisions[name] = arm_candidate(
                candidate,
                first_completed_bar=first_bar,
                execution_bar=execution_bar,
                session_open_at=opened_at,
                equity=Decimal(snapshot["equity"]),
                available_cash=Decimal(snapshot["availableCash"]),
                current_open_heat=Decimal(snapshot["currentOpenHeat"]),
                current_cluster_heat=Decimal(snapshot["currentClusterHeat"]),
                sizing_policy=policy,
            )
            decision = decisions[name]
            self.assertFalse(decision.armed)
            self.assertEqual(decision.reason, expected["authoritativeReason"])
            self.assertIsNone(decision.plan)
            assert decision.detail is not None
            self.assertEqual(
                decision.detail["usableRiskBudget"],
                snapshot["expectedUsableRiskBudget"],
            )
            self.assertEqual(
                decision.detail["limitingFactors"],
                snapshot["expectedLimitingFactors"],
            )
            self.assertEqual(decision.detail["quantity"], expected[f"{name}Quantity"])
            self.assertEqual(
                decision.detail["structuralStopDistance"],
                expected["structuralStopDistance"],
            )
            self.assertEqual(
                decision.detail["atrStopFloor"], expected["atrStopFloor"]
            )
            self.assertEqual(
                decision.detail["effectiveStopDistance"],
                expected["effectiveStopDistance"],
            )

        gap = first_bar.open_price / candidate.close_price - Decimal(1)
        self.assertEqual(gap, Decimal(expected["gapRate"]))
        entry = execution_bar.close_price * (
            Decimal(1) + ADVERSE_SLIPPAGE.entry_rate
        )
        exit_reference = execution_bar.close_price - Decimal(
            expected["effectiveStopDistance"]
        )
        exit_price = exit_reference * (
            Decimal(1) - ADVERSE_SLIPPAGE.exit_rate
        )
        buy = TradeSignal("one-share-buy", symbol, Side.BUY, entry, Decimal(1), "shadow")
        sell = TradeSignal(
            "one-share-sell", symbol, Side.SELL, exit_price, Decimal(1), "shadow"
        )
        one_share_loss = (
            entry
            - exit_price
            + toss_trade_costs(buy).total
            + toss_trade_costs(sell).total
        )
        self.assertEqual(
            one_share_loss,
            Decimal(expected["oneShareEstimatedLossWith5bpAndCosts"]),
        )
        self.assertGreater(
            one_share_loss,
            Decimal(source["hermesSnapshot"]["expectedUsableRiskBudget"]),
        )


if __name__ == "__main__":
    unittest.main()
