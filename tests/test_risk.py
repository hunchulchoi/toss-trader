import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Side, TradeSignal
from toss_trader.risk import (
    RiskContext,
    RiskLimits,
    RiskManager,
    UniverseCandidateRisk,
    UniverseRiskContext,
)

NOW = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)


def signal(**overrides: object) -> TradeSignal:
    values: dict[str, object] = {
        "signal_id": "ma-005930-20260812T1400",
        "symbol": "005930",
        "side": Side.BUY,
        "reference_price": Decimal(71000),
        "quantity": Decimal(3),
        "reason": "MA20 crossed above MA60",
    }
    values.update(overrides)
    return TradeSignal(**values)  # type: ignore[arg-type]


class RiskManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = RiskManager(RiskLimits())

    def test_approves_small_new_buy(self) -> None:
        decision = self.manager.evaluate(
            signal(),
            RiskContext(now=NOW, market_close_at=NOW + timedelta(hours=1)),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.violations, ())

    def test_hermes_rejection_includes_rationale(self) -> None:
        decision = self.manager.evaluate(
            signal(),
            RiskContext(
                now=NOW,
                advisor_status="rejected",
                advisor_rationale="단기 거래량이 평균보다 낮아 신호 강도가 약함",
            ),
        )

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.violations,
            ("Hermes 거부: 단기 거래량이 평균보다 낮아 신호 강도가 약함",),
        )

    def test_rejects_every_configured_safety_limit(self) -> None:
        decision = self.manager.evaluate(
            signal(quantity=Decimal(5)),
            RiskContext(
                now=NOW,
                market_close_at=NOW + timedelta(minutes=5),
                position_notional=Decimal(800000),
                daily_buy_count=5,
                daily_return_rate=Decimal("-0.04"),
                consecutive_api_errors=5,
                seen_signal_ids=frozenset({"ma-005930-20260812T1400"}),
            ),
        )

        self.assertFalse(decision.approved)
        self.assertEqual(
            set(decision.violations),
            {
                "duplicate-signal",
                "max-order-notional",
                "max-position-notional",
                "max-daily-buys",
                "daily-loss-limit",
                "api-error-kill-switch",
                "market-close-window",
            },
        )

    def test_market_close_buy_rule_does_not_block_sell(self) -> None:
        decision = self.manager.evaluate(
            signal(side=Side.SELL),
            RiskContext(
                now=NOW,
                market_close_at=NOW + timedelta(minutes=5),
                position_quantity=Decimal(3),
            ),
        )

        self.assertTrue(decision.approved)

    def test_rejects_all_trades_on_market_holiday(self) -> None:
        decision = self.manager.evaluate(
            signal(side=Side.SELL),
            RiskContext(
                now=NOW,
                market_is_business_day=False,
                position_quantity=Decimal(3),
            ),
        )

        self.assertFalse(decision.approved)
        self.assertIn("market-closed", decision.violations)

    def test_rejects_buy_larger_than_available_paper_cash(self) -> None:
        decision = self.manager.evaluate(
            signal(),
            RiskContext(now=NOW, available_cash=Decimal(200000)),
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient-paper-cash", decision.violations)

    def test_rejects_sell_larger_than_position(self) -> None:
        decision = self.manager.evaluate(
            signal(side=Side.SELL, quantity=Decimal(3)),
            RiskContext(now=NOW, position_quantity=Decimal(2)),
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient-position", decision.violations)

    def test_audits_dynamic_universe_candidate_safety(self) -> None:
        decision = self.manager.evaluate_universe_candidate(
            UniverseCandidateRisk(
                symbol="069500",
                reference_price=Decimal(400000),
                security_type="ETF",
                is_common_share=False,
                status="ACTIVE",
                trading_suspended=True,
            ),
            UniverseRiskContext(
                quantity=Decimal(1),
                available_cash=Decimal(200000),
                daily_return_rate=Decimal("-0.04"),
                consecutive_api_errors=5,
            ),
        )

        self.assertFalse(decision.approved)
        self.assertEqual(
            set(decision.violations),
            {
                "unsupported-security-type",
                "not-common-share",
                "trading-suspended",
                "max-order-notional",
                "insufficient-paper-cash",
                "daily-loss-limit",
                "api-error-kill-switch",
            },
        )

    def test_universe_failure_blocks_buy_but_not_sell(self) -> None:
        buy = self.manager.evaluate(
            signal(), RiskContext(now=NOW, new_buys_allowed=False)
        )
        sell = self.manager.evaluate(
            signal(side=Side.SELL),
            RiskContext(
                now=NOW, position_quantity=Decimal(3), new_buys_allowed=False
            ),
        )

        self.assertIn("universe-refresh-failed", buy.violations)
        self.assertTrue(sell.approved)

    def test_rejects_new_position_when_portfolio_is_full(self) -> None:
        decision = self.manager.evaluate(
            signal(), RiskContext(now=NOW, open_position_count=5)
        )

        self.assertFalse(decision.approved)
        self.assertIn("max-open-positions", decision.violations)


if __name__ == "__main__":
    unittest.main()
