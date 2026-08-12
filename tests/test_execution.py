import unittest
from datetime import UTC, datetime
from decimal import Decimal

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
        self.assertEqual(self.ledger.daily_buy_count(self.now.date()), 0)


if __name__ == "__main__":
    unittest.main()
