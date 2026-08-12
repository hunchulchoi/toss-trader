import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from toss_trader.execution import PaperTradingService
from toss_trader.models import Side, TradeSignal
from toss_trader.paper import PaperLedger
from toss_trader.risk import RiskLimits, RiskManager


class PaperTradingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = PaperLedger(":memory:")
        self.service = PaperTradingService(
            ledger=self.ledger,
            risk_manager=RiskManager(RiskLimits()),
        )
        self.now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.ledger.close()

    def test_approved_signal_is_recorded(self) -> None:
        result = self.service.submit(
            TradeSignal(
                signal_id="approved-1",
                symbol="005930",
                side=Side.BUY,
                reference_price=Decimal(71000),
                quantity=Decimal(1),
                reason="test",
            ),
            now=self.now,
        )

        self.assertTrue(result.decision.approved)
        self.assertIsNotNone(result.fill)
        self.assertTrue(result.decision_id)
        decisions = self.ledger.recent_risk_decisions()
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["approved"])
        self.assertEqual(decisions[0]["availableCash"], "1000000")

    def test_rejected_signal_is_not_recorded(self) -> None:
        result = self.service.submit(
            TradeSignal(
                signal_id="too-large",
                symbol="005930",
                side=Side.BUY,
                reference_price=Decimal(71000),
                quantity=Decimal(10),
                reason="test",
            ),
            now=self.now,
        )

        self.assertFalse(result.decision.approved)
        self.assertIsNone(result.fill)
        self.assertFalse(self.ledger.recent_risk_decisions()[0]["approved"])
        self.assertEqual(self.ledger.daily_buy_count(self.now.date()), 0)

    def test_audit_failure_prevents_fill(self) -> None:
        signal = TradeSignal(
            signal_id="audit-write-failure",
            symbol="005930",
            side=Side.BUY,
            reference_price=Decimal(70000),
            quantity=Decimal(1),
            reason="audit must precede fill",
        )

        with (
            patch.object(
                self.ledger,
                "record_risk_decision",
                side_effect=RuntimeError("audit unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "audit unavailable"),
        ):
            self.service.submit(signal, now=self.now)

        self.assertEqual(self.ledger.seen_signal_ids(), set())

    def test_rejects_buy_after_initial_cash_is_spent(self) -> None:
        service = PaperTradingService(
            ledger=self.ledger,
            risk_manager=RiskManager(RiskLimits()),
            initial_cash=Decimal(100000),
        )
        first = service.submit(
            TradeSignal(
                signal_id="cash-buy-1",
                symbol="005930",
                side=Side.BUY,
                reference_price=Decimal(71000),
                quantity=Decimal(1),
                reason="test",
            ),
            now=self.now,
        )
        second = service.submit(
            TradeSignal(
                signal_id="cash-buy-2",
                symbol="000660",
                side=Side.BUY,
                reference_price=Decimal(50000),
                quantity=Decimal(1),
                reason="test",
            ),
            now=self.now,
        )

        self.assertTrue(first.decision.approved)
        self.assertFalse(second.decision.approved)
        self.assertIn("insufficient-paper-cash", second.decision.violations)
        self.assertEqual(self.ledger.cash_balance(Decimal(100000)), Decimal(29000))
        decisions = self.ledger.recent_risk_decisions()
        self.assertEqual(len(decisions), 2)
        self.assertFalse(decisions[0]["approved"])
        self.assertEqual(
            decisions[0]["violations"], ["insufficient-paper-cash"]
        )


if __name__ == "__main__":
    unittest.main()
