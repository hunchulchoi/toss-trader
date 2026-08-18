from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from toss_trader.models import Candle
from toss_trader.setup_screening import (
    FlowObservation,
    SetupContext,
    SetupType,
    evaluate_setup,
)
from toss_trader.v2_engine import (
    ADVERSE_SLIPPAGE,
    GAP_UP_THRESHOLD,
    arm_candidate,
    build_daily_candidate,
    pullback_invalidated,
    stop_touched,
    wilder_atr,
)

SEOUL = ZoneInfo("Asia/Seoul")


def daily_candles(closes: list[int], *, opens: list[int] | None = None) -> list[Candle]:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    open_prices = opens or closes
    return [
        Candle(
            symbol="005930",
            interval="1d",
            timestamp=started_at + timedelta(days=index),
            open_price=Decimal(open_prices[index]),
            high_price=Decimal(max(open_prices[index], close) + 1),
            low_price=Decimal(min(open_prices[index], close) - 1),
            close_price=Decimal(close),
            volume=Decimal(1000),
            currency="KRW",
        )
        for index, close in enumerate(closes)
    ]


def pullback_candles() -> list[Candle]:
    return daily_candles([*range(100, 250), *([249, 251] * 25)])


def flow_observations(signal_session: date) -> tuple[FlowObservation, ...]:
    return tuple(
        FlowObservation(
            symbol="005930",
            session_index=index,
            session_date=signal_session - timedelta(days=5 - index),
            available_at=datetime.combine(
                signal_session - timedelta(days=5 - index), time(18), tzinfo=UTC
            ),
            foreign_net_buy=Decimal(foreign),
            institutional_net_buy=Decimal(0),
            trading_value=Decimal(100),
        )
        for index, foreign in enumerate((-10, -10, -10, -10, -10, 60))
    )


def approved_context(candle: Candle) -> SetupContext:
    return SetupContext(
        decision_at=candle.timestamp + timedelta(hours=20),
        signal_session=candle.timestamp.date(),
        flow_observations=flow_observations(candle.timestamp.date()),
        event_imminent=False,
        gap_up_chase=False,
    )


class V2EngineTest(unittest.TestCase):
    def test_build_daily_candidate_reuses_evaluate_setup_and_atr(self) -> None:
        history = pullback_candles()
        context = approved_context(history[-1])

        candidate = build_daily_candidate(history, context=context)

        self.assertEqual(candidate.decision, evaluate_setup(history, context=context))
        self.assertTrue(candidate.decision.approved)
        self.assertIn(SetupType.PULLBACK, candidate.decision.setups)
        self.assertEqual(candidate.setup_low, history[-1].low_price)
        self.assertEqual(candidate.close_price, history[-1].close_price)
        self.assertEqual(candidate.atr14, wilder_atr(history, 14))

    def test_wilder_atr14_smoothes_true_range(self) -> None:
        history = daily_candles([10] * 16)

        self.assertEqual(wilder_atr(history, 14), Decimal(2))

    def test_arm_cancels_gap_of_three_percent_or_more(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )
        open_price = candidate.close_price * (Decimal(1) + GAP_UP_THRESHOLD)

        decision = arm_candidate(
            candidate,
            first_one_minute_open=open_price,
            equity=Decimal(1_000_000),
            available_cash=Decimal(1_000_000),
        )

        self.assertFalse(decision.armed)
        self.assertEqual(decision.reason, "violation:gap-up-chase")
        self.assertIsNone(decision.plan)

    def test_arm_applies_five_bps_and_persists_effective_stop(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )
        open_price = candidate.close_price

        decision = arm_candidate(
            candidate,
            first_one_minute_open=open_price,
            equity=Decimal(1_000_000),
            available_cash=Decimal(1_000_000),
        )

        self.assertTrue(decision.armed)
        assert decision.plan is not None
        self.assertEqual(
            decision.plan.entry_price,
            open_price * (Decimal(1) + ADVERSE_SLIPPAGE.entry_rate),
        )
        self.assertGreater(decision.plan.quantity, 0)
        self.assertLess(decision.plan.stop_price, open_price)
        self.assertLessEqual(decision.plan.stop_price, candidate.setup_low)

    def test_arm_does_not_promote_zero_quantity(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )

        decision = arm_candidate(
            candidate,
            first_one_minute_open=candidate.close_price,
            equity=Decimal(1_000_000),
            available_cash=Decimal(1_000_000),
            current_open_heat=Decimal(20_000),
        )

        self.assertFalse(decision.armed)
        self.assertEqual(decision.reason, "violation:below-one-lot")
        self.assertIsNone(decision.plan)

    def test_stop_touched_uses_completed_bar_low(self) -> None:
        self.assertTrue(stop_touched(bar_low=Decimal(100), stop_price=Decimal(100)))
        self.assertTrue(stop_touched(bar_low=Decimal(99), stop_price=Decimal(100)))
        self.assertFalse(stop_touched(bar_low=Decimal(101), stop_price=Decimal(100)))

    def test_pullback_invalidated_only_for_pullback_close_below_ma50(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )

        self.assertTrue(
            pullback_invalidated(candidate, close_price=candidate.ma50 - Decimal("0.0001"))
        )
        self.assertFalse(pullback_invalidated(candidate, close_price=candidate.ma50))

    def test_non_pullback_is_not_invalidated_by_ma50(self) -> None:
        history = daily_candles([*[10] * 180, *range(11, 31)])
        last = history[-1]
        context = SetupContext(
            decision_at=last.timestamp + timedelta(hours=20),
            signal_session=last.timestamp.date(),
            flow_observations=flow_observations(last.timestamp.date()),
            event_imminent=False,
            gap_up_chase=False,
        )
        candidate = build_daily_candidate(history, context=context)

        self.assertNotIn(SetupType.PULLBACK, candidate.decision.setups)
        self.assertFalse(
            pullback_invalidated(candidate, close_price=candidate.ma50 - Decimal(1))
        )

    def test_module_has_no_time_or_rsi70_exit(self) -> None:
        import toss_trader.v2_engine as engine

        self.assertFalse(hasattr(engine, "time_exit"))
        self.assertFalse(hasattr(engine, "rsi_take_profit"))


if __name__ == "__main__":
    unittest.main()
