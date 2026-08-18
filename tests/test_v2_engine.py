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
    ArmedTradePlan,
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


def session_open_after(signal_session: date) -> datetime:
    return datetime.combine(signal_session + timedelta(days=1), time(9), tzinfo=SEOUL)


def minute_bar(
    *,
    open_price: Decimal,
    session_open_at: datetime,
    interval: str = "1m",
    timestamp: datetime | None = None,
) -> Candle:
    return Candle(
        symbol="005930",
        interval=interval,
        timestamp=timestamp or session_open_at,
        open_price=open_price,
        high_price=open_price + Decimal(1),
        low_price=open_price - Decimal(1),
        close_price=open_price,
        volume=Decimal(1000),
        currency="KRW",
    )


def arm(
    candidate,
    *,
    open_price: Decimal | None = None,
    session_open_at: datetime | None = None,
    bar: Candle | None = None,
    equity: Decimal = Decimal(1_000_000),
    available_cash: Decimal = Decimal(1_000_000),
    current_open_heat: Decimal = Decimal(0),
):
    opened_at = session_open_at or session_open_after(candidate.signal_session)
    return arm_candidate(
        candidate,
        first_completed_bar=bar
        or minute_bar(
            open_price=open_price or candidate.close_price,
            session_open_at=opened_at,
        ),
        session_open_at=opened_at,
        equity=equity,
        available_cash=available_cash,
        current_open_heat=current_open_heat,
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

        decision = arm(candidate, open_price=open_price)

        self.assertFalse(decision.armed)
        self.assertEqual(decision.reason, "setup-v2:violation:gap-up-chase")
        self.assertIsNone(decision.plan)

    def test_arm_applies_five_bps_and_persists_plan_fields(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )
        open_price = candidate.close_price

        decision = arm(candidate, open_price=open_price)

        self.assertTrue(decision.armed)
        self.assertEqual(decision.reason, "setup-v2:armed")
        assert decision.plan is not None
        self.assertEqual(
            decision.plan.entry_price,
            open_price * (Decimal(1) + ADVERSE_SLIPPAGE.entry_rate),
        )
        self.assertGreater(decision.plan.quantity, 0)
        self.assertLess(decision.plan.stop_price, open_price)
        self.assertLessEqual(decision.plan.stop_price, candidate.setup_low)
        self.assertEqual(decision.plan.setup_session, candidate.signal_session)
        self.assertEqual(decision.plan.ma50, candidate.ma50)
        self.assertEqual(decision.plan.signal_close, candidate.close_price)

    def test_arm_does_not_promote_zero_quantity(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )

        decision = arm(candidate, current_open_heat=Decimal(20_000))

        self.assertFalse(decision.armed)
        self.assertEqual(decision.reason, "setup-v2:violation:below-one-lot")
        self.assertIsNone(decision.plan)

    def test_arm_rejects_daily_bar_naive_time_and_timestamp_mismatch(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )
        opened_at = session_open_after(candidate.signal_session)
        with self.assertRaisesRegex(ValueError, "1m candle"):
            arm(candidate, bar=history[-1], session_open_at=opened_at)
        naive = datetime(2026, 1, 2, 9, 0)
        with self.assertRaisesRegex(ValueError, "timezone"):
            arm_candidate(
                candidate,
                first_completed_bar=minute_bar(
                    open_price=candidate.close_price,
                    session_open_at=opened_at,
                ),
                session_open_at=naive,
                equity=Decimal(1_000_000),
                available_cash=Decimal(1_000_000),
            )
        with self.assertRaisesRegex(ValueError, "timestamp"):
            arm(
                candidate,
                bar=minute_bar(
                    open_price=candidate.close_price,
                    session_open_at=opened_at,
                    timestamp=opened_at + timedelta(minutes=1),
                ),
                session_open_at=opened_at,
            )

    def test_arm_requires_kst_session_after_signal_day(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )
        same_day = datetime.combine(candidate.signal_session, time(9), tzinfo=SEOUL)
        with self.assertRaisesRegex(ValueError, "after the setup signal session"):
            arm(candidate, session_open_at=same_day)
        next_open = session_open_after(candidate.signal_session)
        decision = arm(candidate, session_open_at=next_open)
        self.assertTrue(decision.armed)

    def test_stop_touched_uses_completed_bar_low(self) -> None:
        self.assertTrue(stop_touched(bar_low=Decimal(100), stop_price=Decimal(100)))
        self.assertTrue(stop_touched(bar_low=Decimal(99), stop_price=Decimal(100)))
        self.assertFalse(stop_touched(bar_low=Decimal(101), stop_price=Decimal(100)))

    def test_pullback_invalidated_restarts_from_armed_plan(self) -> None:
        history = pullback_candles()
        candidate = build_daily_candidate(
            history, context=approved_context(history[-1])
        )
        plan = arm(candidate).plan
        assert plan is not None

        self.assertTrue(
            pullback_invalidated(plan, close_price=plan.ma50 - Decimal("0.0001"))
        )
        self.assertFalse(pullback_invalidated(plan, close_price=plan.ma50))

    def test_non_pullback_plan_is_not_invalidated_by_ma50(self) -> None:
        plan = ArmedTradePlan(
            symbol="005930",
            quantity=Decimal(1),
            execution_open=Decimal(100),
            entry_price=Decimal("100.05"),
            stop_price=Decimal(90),
            planned_heat=Decimal(10),
            setups=(SetupType.OVERSOLD_REVERSAL,),
            setup_session=date(2026, 1, 2),
            ma50=Decimal(95),
            signal_close=Decimal(100),
        )

        self.assertFalse(pullback_invalidated(plan, close_price=Decimal(90)))

    def test_module_has_no_time_or_rsi70_exit(self) -> None:
        import toss_trader.v2_engine as engine

        self.assertFalse(hasattr(engine, "time_exit"))
        self.assertFalse(hasattr(engine, "rsi_take_profit"))


if __name__ == "__main__":
    unittest.main()
