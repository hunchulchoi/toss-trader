import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from toss_trader.models import Candle, Side, TradeSignal
from toss_trader.official_data import OfficialDataRepository
from toss_trader.repository import SqliteMarketRepository
from toss_trader.setup_screening import (
    FlowObservation,
    OfficialSetupContextFactory,
    PositionSizingPolicy,
    SetupContext,
    SetupDecision,
    SetupType,
    SlippageAssumption,
    StrictSetupV2EntryGate,
    ValuationEvidence,
    ValuationTier,
    evaluate_setup,
    hermes_experimental_can_arm,
    position_size_reference,
    summarize_flow,
    valuation_tier,
)

SEOUL = ZoneInfo("Asia/Seoul")


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


def flow_observations(
    signal_session: date,
    *,
    foreign: tuple[int, ...] = (-10, -10, -10, -10, -10, 60),
    institutional: tuple[int, ...] = (0, 0, 0, 0, 0, 0),
    final_available_at: datetime | None = None,
) -> tuple[FlowObservation, ...]:
    result = []
    for index, (foreign_net, institutional_net) in enumerate(
        zip(foreign, institutional, strict=True)
    ):
        session = signal_session - timedelta(days=5 - index)
        available_at = datetime.combine(session, time(18), tzinfo=UTC)
        if index == 5 and final_available_at is not None:
            available_at = final_available_at
        result.append(
            FlowObservation(
                symbol="005930",
                session_index=index,
                session_date=session,
                available_at=available_at,
                foreign_net_buy=Decimal(foreign_net),
                institutional_net_buy=Decimal(institutional_net),
                trading_value=Decimal(100),
            )
        )
    return tuple(result)


def approved_context(candle: Candle) -> SetupContext:
    decision_at = candle.timestamp + timedelta(hours=20)
    return SetupContext(
        decision_at=decision_at,
        signal_session=candle.timestamp.date(),
        flow_observations=flow_observations(candle.timestamp.date()),
        event_imminent=False,
        gap_up_chase=False,
    )


class FlowSummaryTest(unittest.TestCase):
    def test_detects_normalized_five_day_reversal_from_six_sessions(self) -> None:
        signal_session = date(2026, 1, 9)
        result = summarize_flow(
            flow_observations(signal_session),
            symbol="005930",
            signal_session=signal_session,
            decision_at=datetime(2026, 1, 9, 20, tzinfo=UTC),
        )

        assert result is not None
        self.assertEqual(result.previous_5d_ratio, Decimal("-0.1000"))
        self.assertEqual(result.current_5d_ratio, Decimal("0.0400"))
        self.assertTrue(result.foreign_reversal)

    def test_requires_latest_session_to_be_net_buy(self) -> None:
        signal_session = date(2026, 1, 9)
        result = summarize_flow(
            flow_observations(
                signal_session,
                foreign=(-30, -30, -30, 100, 100, -1),
            ),
            symbol="005930",
            signal_session=signal_session,
            decision_at=datetime(2026, 1, 9, 20, tzinfo=UTC),
        )

        assert result is not None
        self.assertGreater(result.current_5d_ratio, 0)
        self.assertFalse(result.foreign_reversal)

    def test_excludes_flow_not_available_at_decision_time(self) -> None:
        signal_session = date(2026, 1, 9)
        result = summarize_flow(
            flow_observations(
                signal_session,
                final_available_at=datetime(2026, 1, 10, tzinfo=UTC),
            ),
            symbol="005930",
            signal_session=signal_session,
            decision_at=datetime(2026, 1, 9, 20, tzinfo=UTC),
        )

        self.assertIsNone(result)

    def test_falls_back_to_latest_available_session(self) -> None:
        signal_session = date(2026, 1, 10)
        available = list(flow_observations(signal_session - timedelta(days=1)))
        available.append(
            FlowObservation(
                symbol="005930",
                session_index=6,
                session_date=signal_session,
                available_at=datetime(2026, 1, 11, tzinfo=UTC),
                foreign_net_buy=Decimal(10),
                institutional_net_buy=Decimal(0),
                trading_value=Decimal(100),
            )
        )

        result = summarize_flow(
            tuple(available),
            symbol="005930",
            signal_session=signal_session,
            decision_at=datetime(2026, 1, 10, 12, tzinfo=UTC),
        )

        assert result is not None
        self.assertEqual(result.latest_session, signal_session - timedelta(days=1))
        self.assertTrue(result.foreign_reversal)

    def test_rejects_gap_created_by_unavailable_middle_session(self) -> None:
        signal_session = date(2026, 1, 10)
        observations = list(flow_observations(signal_session))
        observations.insert(
            0,
            FlowObservation(
                symbol="005930",
                session_index=-1,
                session_date=signal_session - timedelta(days=6),
                available_at=datetime(2026, 1, 4, 18, tzinfo=UTC),
                foreign_net_buy=Decimal(-10),
                institutional_net_buy=Decimal(0),
                trading_value=Decimal(100),
            ),
        )
        middle = observations[3]
        observations[3] = FlowObservation(
            symbol=middle.symbol,
            session_index=middle.session_index,
            session_date=middle.session_date,
            available_at=datetime(2026, 1, 11, tzinfo=UTC),
            foreign_net_buy=middle.foreign_net_buy,
            institutional_net_buy=middle.institutional_net_buy,
            trading_value=middle.trading_value,
        )

        result = summarize_flow(
            tuple(observations),
            symbol="005930",
            signal_session=signal_session,
            decision_at=datetime(2026, 1, 10, 20, tzinfo=UTC),
        )

        self.assertIsNone(result)

    def test_uses_pooled_trading_value_normalization(self) -> None:
        signal_session = date(2026, 1, 9)
        foreign = (-100, 1, 1, 1, 1, 100)
        trading_values = (10000, 1, 1, 1, 1, 100)
        observations = tuple(
            FlowObservation(
                symbol="005930",
                session_index=index,
                session_date=signal_session - timedelta(days=5 - index),
                available_at=datetime.combine(
                    signal_session - timedelta(days=5 - index),
                    time(18),
                    tzinfo=UTC,
                ),
                foreign_net_buy=Decimal(foreign[index]),
                institutional_net_buy=Decimal(0),
                trading_value=Decimal(trading_values[index]),
            )
            for index in range(6)
        )

        result = summarize_flow(
            observations,
            symbol="005930",
            signal_session=signal_session,
            decision_at=datetime(2026, 1, 9, 20, tzinfo=UTC),
        )

        assert result is not None
        self.assertLess(result.previous_5d_ratio, 0)
        self.assertGreater(result.current_5d_ratio, 0)
        self.assertTrue(result.foreign_reversal)

    def test_rejects_invalid_flow_contracts(self) -> None:
        signal_session = date(2026, 1, 9)
        valid = list(flow_observations(signal_session))
        cases = {
            "symbol": [
                *valid[:-1],
                FlowObservation(
                    symbol="000660",
                    session_index=valid[-1].session_index,
                    session_date=valid[-1].session_date,
                    available_at=valid[-1].available_at,
                    foreign_net_buy=valid[-1].foreign_net_buy,
                    institutional_net_buy=valid[-1].institutional_net_buy,
                    trading_value=valid[-1].trading_value,
                ),
            ],
            "increasing": [valid[1], valid[0], *valid[2:]],
            "positive": [
                *valid[:-1],
                FlowObservation(
                    symbol=valid[-1].symbol,
                    session_index=valid[-1].session_index,
                    session_date=valid[-1].session_date,
                    available_at=valid[-1].available_at,
                    foreign_net_buy=valid[-1].foreign_net_buy,
                    institutional_net_buy=valid[-1].institutional_net_buy,
                    trading_value=Decimal(0),
                ),
            ],
        }
        for message, observations in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                summarize_flow(
                    tuple(observations),
                    symbol="005930",
                    signal_session=signal_session,
                    decision_at=datetime(2026, 1, 9, 20, tzinfo=UTC),
                )


class SetupScreeningTest(unittest.TestCase):
    def test_strict_entry_gate_blocks_missing_pit_inputs(self) -> None:
        repository = SqliteMarketRepository(":memory:")
        history = pullback_candles()
        repository.upsert_candles(history)
        signal = TradeSignal(
            signal_id="candidate",
            symbol="005930",
            side=Side.BUY,
            reference_price=history[-1].close_price,
            quantity=Decimal(1),
            reason="candidate",
        )
        try:
            result = StrictSetupV2EntryGate(repository).evaluate(
                signal, history[-1].timestamp + timedelta(hours=20)
            )
        finally:
            repository.close()

        self.assertFalse(result.approved)
        self.assertEqual(
            result.reason,
            "setup-v2:missing:flow-history,missing:event-calendar",
        )

    def test_strict_entry_gate_passes_complete_setup(self) -> None:
        repository = SqliteMarketRepository(":memory:")
        history = pullback_candles()
        repository.upsert_candles(history)
        signal = TradeSignal(
            signal_id="candidate",
            symbol="005930",
            side=Side.BUY,
            reference_price=history[-1].close_price,
            quantity=Decimal(1),
            reason="candidate",
        )

        def context_factory(
            symbol: str,
            signal_session: date,
            decision_at: datetime,
            gap_up: bool,
        ) -> SetupContext:
            self.assertEqual(symbol, "005930")
            self.assertFalse(gap_up)
            return SetupContext(
                decision_at=decision_at,
                signal_session=signal_session,
                flow_observations=flow_observations(signal_session),
                event_imminent=False,
                gap_up_chase=gap_up,
            )

        try:
            result = StrictSetupV2EntryGate(
                repository, context_factory=context_factory
            ).evaluate(signal, history[-1].timestamp + timedelta(hours=20))
        finally:
            repository.close()

        self.assertTrue(result.approved)
        self.assertIsNone(result.reason)

    def test_approves_pullback_with_independent_flow_reversal(self) -> None:
        history = pullback_candles()
        result = evaluate_setup(history, context=approved_context(history[-1]))

        self.assertTrue(result.approved)
        self.assertEqual(
            result.setups, (SetupType.PULLBACK, SetupType.FLOW_REVERSAL)
        )
        self.assertEqual(result.flow_stars, 1)

    def test_price_setups_cannot_replace_flow_confirmation(self) -> None:
        history = pullback_candles()
        result = evaluate_setup(
            history,
            context=SetupContext(
                decision_at=history[-1].timestamp + timedelta(hours=20),
                signal_session=history[-1].timestamp.date(),
                event_imminent=False,
                gap_up_chase=False,
            ),
        )

        self.assertFalse(result.approved)
        self.assertIn("flow-history", result.missing_checks)

    def test_flow_cannot_replace_price_setup(self) -> None:
        history = candles([200] * 200)
        result = evaluate_setup(history, context=approved_context(history[-1]))

        self.assertFalse(result.approved)
        self.assertIn("missing-price-setup", result.violations)

    def test_hermes_experimental_allows_price_reference_violations_only(self) -> None:
        def decision(
            *,
            approved: bool = False,
            violations: tuple[str, ...] = (),
            missing_checks: tuple[str, ...] = (),
        ) -> SetupDecision:
            return SetupDecision(
                symbol="005930",
                approved=approved,
                setups=(),
                violations=violations,
                missing_checks=missing_checks,
                rsi14=Decimal(50),
                ma50=Decimal(100),
                ma200=Decimal(90),
                ma50_distance=Decimal("0.01"),
                flow_stars=0,
                flow_summary=None,
                valuation_tier=ValuationTier.B,
                confidence_multiplier=Decimal(1),
                proposed_confidence_multiplier=Decimal(1),
            )

        self.assertTrue(hermes_experimental_can_arm(decision(approved=True)))
        self.assertTrue(
            hermes_experimental_can_arm(
                decision(violations=("missing-price-setup", "rsi-chase"))
            )
        )
        self.assertFalse(
            hermes_experimental_can_arm(decision(violations=("event-imminent",)))
        )
        self.assertFalse(
            hermes_experimental_can_arm(decision(missing_checks=("flow-history",)))
        )

    def test_complete_flow_history_without_reversal_is_soft_confirmation(self) -> None:
        history = pullback_candles()
        context = approved_context(history[-1])
        result = evaluate_setup(
            history,
            context=SetupContext(
                decision_at=context.decision_at,
                signal_session=context.signal_session,
                flow_observations=flow_observations(
                    context.signal_session,
                    foreign=(10, 10, 10, 10, 10, 10),
                ),
                event_imminent=False,
                gap_up_chase=False,
            ),
        )

        self.assertTrue(result.approved)
        self.assertNotIn("flow-not-confirmed", result.violations)
        self.assertEqual(result.flow_stars, 0)
        self.assertIsNotNone(result.flow_summary)

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
            confirmed, context=approved_context(confirmed[-1])
        )

        self.assertLessEqual(result.rsi14, Decimal(35))
        self.assertIn(SetupType.OVERSOLD_REVERSAL, result.setups)
        self.assertTrue(result.approved)

    def test_institutional_flow_adds_star_without_changing_approval(self) -> None:
        history = pullback_candles()
        context = approved_context(history[-1])
        institutional = (0, 0, 0, 0, 0, 10)
        result = evaluate_setup(
            history,
            context=SetupContext(
                decision_at=context.decision_at,
                signal_session=history[-1].timestamp.date(),
                flow_observations=flow_observations(
                    history[-1].timestamp.date(), institutional=institutional
                ),
                event_imminent=False,
                gap_up_chase=False,
            ),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.flow_stars, 2)

    def test_official_context_reads_covered_events_and_valid_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/market.db"
            repository = OfficialDataRepository(path)
            repository.record_coverage(
                dataset="events",
                start=date(2026, 8, 1),
                end=date(2026, 8, 18),
                completed_at="2026-08-18T01:00:00+00:00",
                source="opendart:list",
                row_count=1,
            )
            repository.upsert_events(
                [{
                    "symbol": "005930", "corp_code": "00126380",
                    "receipt_no": "20260818000001", "receipt_date": "2026-08-18",
                    "report_name": "유상증자결정",
                    "available_at": "2026-08-18T08:00:00+09:00",
                    "blocked_through": "2026-08-21T08:00:00+09:00",
                    "is_entry_blocking": 1, "is_preannounced": 0,
                    "scheduled_for": None, "source": "opendart:list",
                    "retrieved_at": "2026-08-18T01:00:00+00:00",
                    "payload_hash": "event",
                }]
            )
            repository.close()
            connection = sqlite3.connect(path)
            connection.executemany(
                "INSERT INTO market_flow_pit_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "005930", f"2026-08-{day:02d}", index,
                        f"2026-08-{day + 1:02d}T08:00:00+09:00",
                        str(-10 if index < 5 else 60), "10", "1000",
                        "krx:investor-trading", f"005930:{day}",
                        "2026-08-18T01:00:00+00:00", str(day),
                    )
                    for index, day in enumerate(range(11, 17), start=1)
                ],
            )
            connection.executemany(
                "INSERT INTO market_flow_pit_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "005930", f"2026-08-{day:02d}", index,
                        f"2026-08-{day + 1:02d}T08:00:00+09:00",
                        "999", "999", "1000", "kis:FHPTJ04160001",
                        f"005930:{day}:kis", "2026-08-18T01:00:00+00:00",
                        f"kis-{day}",
                    )
                    for index, day in enumerate(range(11, 17), start=1)
                ],
            )
            connection.commit()
            connection.close()

            context = OfficialSetupContextFactory(path)(
                "005930",
                date(2026, 8, 18),
                datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
                False,
            )

            self.assertTrue(context.event_imminent)
            self.assertEqual(len(context.flow_observations), 6)
            self.assertEqual(context.flow_observations[-1].foreign_net_buy, Decimal(60))

    def test_unknown_preannounced_schedule_blocks_until_realized_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/market.db"
            repository = OfficialDataRepository(path)
            repository.record_coverage(
                dataset="events",
                start=date(2026, 8, 1),
                end=date(2026, 8, 18),
                completed_at="2026-08-18T01:00:00+00:00",
                source="opendart:list",
                row_count=1,
            )
            repository.upsert_events(
                [{
                    "symbol": "005930", "corp_code": "00126380",
                    "receipt_no": "20260810000001", "receipt_date": "2026-08-10",
                    "report_name": "결산실적공시예고",
                    "available_at": "2026-08-10T08:00:00+09:00",
                    "blocked_through": None,
                    "is_entry_blocking": 0, "is_preannounced": 1,
                    "scheduled_for": None, "source": "opendart:list",
                    "retrieved_at": "2026-08-10T01:00:00+00:00",
                    "payload_hash": "preannounce",
                }]
            )
            repository.close()

            context = OfficialSetupContextFactory(path)(
                "005930", date(2026, 8, 18),
                datetime(2026, 8, 18, 15, 0, tzinfo=SEOUL), False,
            )

            self.assertTrue(context.event_imminent)

    def test_fails_closed_when_manual_safety_checks_are_missing(self) -> None:
        history = pullback_candles()
        context = approved_context(history[-1])
        result = evaluate_setup(
            history,
            context=SetupContext(
                decision_at=context.decision_at,
                signal_session=history[-1].timestamp.date(),
                flow_observations=context.flow_observations,
            ),
        )

        self.assertFalse(result.approved)
        self.assertEqual(
            result.missing_checks, ("event-calendar", "gap-up-review")
        )

    def test_valuation_tier_is_diagnostic_only(self) -> None:
        self.assertEqual(
            valuation_tier(
                ValuationEvidence(
                    forward_eps_growth=Decimal("0.25"),
                    sector_per_percentile=Decimal("0.30"),
                )
            ),
            ValuationTier.A,
        )
        history = pullback_candles()
        context = approved_context(history[-1])
        result = evaluate_setup(
            history,
            context=SetupContext(
                decision_at=context.decision_at,
                signal_session=history[-1].timestamp.date(),
                flow_observations=context.flow_observations,
                valuation=ValuationEvidence(
                    forward_eps_growth=Decimal("0.25"),
                    sector_per_percentile=Decimal("0.30"),
                ),
                event_imminent=False,
                gap_up_chase=False,
            ),
        )

        self.assertEqual(result.valuation_tier, ValuationTier.A)
        self.assertEqual(result.confidence_multiplier, Decimal("1.0"))
        self.assertEqual(result.proposed_confidence_multiplier, Decimal("1.5"))


class PositionSizingTest(unittest.TestCase):
    def test_sizes_integer_quantity_with_heat_costs_and_slippage(self) -> None:
        result = position_size_reference(
            symbol="005930",
            equity=Decimal(1000000),
            reference_price=Decimal(10000),
            stop_price=Decimal(9600),
            atr=Decimal(200),
            available_cash=Decimal(1000000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
            slippage=SlippageAssumption(
                entry_rate=Decimal("0.001"), exit_rate=Decimal("0.002")
            ),
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.quantity, Decimal(11))
        self.assertEqual(result.executable_notional, Decimal(110000))
        self.assertLessEqual(result.planned_heat, Decimal(5000))
        self.assertIn("per-trade-risk", result.limiting_factors)

    def test_blocks_when_open_or_cluster_heat_is_exhausted(self) -> None:
        common = {
            "symbol": "005930",
            "equity": Decimal(1000000),
            "reference_price": Decimal(10000),
            "stop_price": Decimal(9600),
            "atr": Decimal(200),
            "available_cash": Decimal(1000000),
        }
        open_blocked = position_size_reference(
            **common,
            current_open_heat=Decimal(20000),
            current_cluster_heat=Decimal(0),
        )
        cluster_blocked = position_size_reference(
            **common,
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(10000),
        )

        self.assertFalse(open_blocked.approved)
        self.assertIn("open-heat", open_blocked.limiting_factors)
        self.assertFalse(cluster_blocked.approved)
        self.assertIn("cluster-heat", cluster_blocked.limiting_factors)

    def test_atr_floor_can_reduce_quantity(self) -> None:
        common = {
            "symbol": "005930",
            "equity": Decimal(1000000),
            "reference_price": Decimal(10000),
            "stop_price": Decimal(9600),
            "available_cash": Decimal(1000000),
            "current_open_heat": Decimal(0),
            "current_cluster_heat": Decimal(0),
        }
        structural = position_size_reference(**common, atr=Decimal(100))
        atr_limited = position_size_reference(**common, atr=Decimal(400))

        self.assertEqual(structural.effective_stop_distance, Decimal(400))
        self.assertEqual(atr_limited.effective_stop_distance, Decimal(600))
        self.assertLess(atr_limited.quantity, structural.quantity)

    def test_order_cap_and_zero_share_are_enforced(self) -> None:
        generous = PositionSizingPolicy(
            per_trade_risk_rate=Decimal("0.5"),
            max_open_heat_rate=Decimal(1),
            max_cluster_heat_rate=Decimal(1),
        )
        capped = position_size_reference(
            symbol="005930",
            equity=Decimal(1000000),
            reference_price=Decimal(10000),
            stop_price=Decimal(9999),
            atr=Decimal(1),
            available_cash=Decimal(1000000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
            policy=generous,
        )
        zero = position_size_reference(
            symbol="005930",
            equity=Decimal(1000000),
            reference_price=Decimal(800000),
            stop_price=Decimal(790000),
            atr=Decimal(1000),
            available_cash=Decimal(1000000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
        )

        self.assertEqual(capped.quantity, Decimal(70))
        self.assertIn("max-order-notional", capped.limiting_factors)
        self.assertFalse(zero.approved)
        self.assertIn("below-one-lot", zero.limiting_factors)

    def test_entry_slippage_and_cash_cannot_exceed_caps(self) -> None:
        generous = PositionSizingPolicy(
            per_trade_risk_rate=Decimal("0.5"),
            max_open_heat_rate=Decimal(1),
            max_cluster_heat_rate=Decimal(1),
        )
        order_limited = position_size_reference(
            symbol="005930",
            equity=Decimal(1000000),
            reference_price=Decimal(10000),
            stop_price=Decimal(9999),
            atr=Decimal(1),
            available_cash=Decimal(1000000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
            policy=generous,
            slippage=SlippageAssumption(entry_rate=Decimal("0.01")),
        )
        cash_blocked = position_size_reference(
            symbol="005930",
            equity=Decimal(1000000),
            reference_price=Decimal(10000),
            stop_price=Decimal(9600),
            atr=Decimal(200),
            available_cash=Decimal(10000),
            current_open_heat=Decimal(0),
            current_cluster_heat=Decimal(0),
        )

        self.assertEqual(order_limited.quantity, Decimal(69))
        self.assertLessEqual(order_limited.required_cash, Decimal(700000))
        self.assertIn("max-order-notional", order_limited.limiting_factors)
        self.assertFalse(cash_blocked.approved)
        self.assertIn("available-cash", cash_blocked.limiting_factors)
        self.assertIn("below-one-lot", cash_blocked.limiting_factors)


if __name__ == "__main__":
    unittest.main()
