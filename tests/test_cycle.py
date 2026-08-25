import io
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from toss_trader.calendar import MarketCalendarService
from toss_trader.cycle import PaperCycleRunner
from toss_trader.cycle_state import SqliteCycleStateStore
from toss_trader.execution import PaperTradingService
from toss_trader.market_data import MarketCollector, StoredMaStrategy
from toss_trader.models import Candle, Side, TradeSignal
from toss_trader.paper import PaperLedger
from toss_trader.portfolio import PortfolioPerformance
from toss_trader.repository import SqliteMarketRepository
from toss_trader.risk import RiskLimits, RiskManager
from toss_trader.setup_screening import (
    EntryGateDecision,
    SetupDecision,
    SetupType,
    ValuationTier,
)
from toss_trader.v2_engine import DailySetupCandidate


class WatchlistCandleClient:
    def __init__(
        self,
        closes: dict[str, list[Decimal]],
        *,
        closed_countries: frozenset[str] = frozenset(),
        daily_next_before: str | None = None,
    ) -> None:
        self.closes = closes
        self.calls: list[tuple[str, int]] = []
        self.interval_calls: list[tuple[str, str, int]] = []
        self.before_calls: list[tuple[str, str, str | None]] = []
        self.calendar_calls: list[str] = []
        self.closed_countries = closed_countries
        self.daily_next_before = daily_next_before

    def stocks(self, symbols: tuple[str, ...]) -> list[dict]:
        return [{"symbol": symbol, "name": f"Name {symbol}"} for symbol in symbols]

    def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int,
        before: str | None,
        adjusted: bool,
    ) -> dict:
        del adjusted
        self.calls.append((symbol, count))
        self.interval_calls.append((symbol, interval, count))
        self.before_calls.append((symbol, interval, before))
        if symbol not in self.closes:
            raise ValueError("simulated-symbol-error")
        start = datetime(2026, 8, 9, tzinfo=UTC)
        return {
            "candles": [
                {
                    "timestamp": (start + timedelta(days=index)).isoformat(),
                    "openPrice": str(close),
                    "highPrice": str(close),
                    "lowPrice": str(close),
                    "closePrice": str(close),
                    "volume": "100",
                    "currency": "KRW" if symbol.isdigit() else "USD",
                }
                for index, close in enumerate(self.closes[symbol])
            ],
            "nextBefore": (
                self.daily_next_before
                if interval == "1d" and before is None
                else None
            ),
        }

    def market_calendar(self, country: str, *, day=None) -> dict:
        assert day is not None
        self.calendar_calls.append(country)
        if country == "KR":
            return {
                "today": {
                    "date": day.isoformat(),
                    "integrated": (
                        None
                        if country in self.closed_countries
                        else {
                            "regularMarket": {
                                "startTime": f"{day.isoformat()}T09:00:00+09:00",
                                "endTime": f"{day.isoformat()}T18:00:00+09:00",
                            }
                        }
                    ),
                }
            }
        return {
            "today": {
                "date": day.isoformat(),
                "regularMarket": (
                    None
                    if country in self.closed_countries
                    else {
                        "startTime": f"{day.isoformat()}T22:30:00+09:00",
                        "endTime": (
                            datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                            + timedelta(days=1, hours=5)
                        ).isoformat(),
                    }
                ),
            }
        }


class FakeV2CycleStrategy:
    def __init__(self, candidate: DailySetupCandidate, bars: list[Candle]) -> None:
        self.candidate = candidate
        self.bars = bars
        self.build_error: ValueError | None = None

    def build_candidate(self, symbol: str, *, now: datetime) -> DailySetupCandidate:
        del symbol, now
        if self.build_error is not None:
            raise self.build_error
        return self.candidate

    def completed_one_minute_bars(
        self, symbol: str, *, now: datetime
    ) -> tuple[Candle, ...]:
        del symbol, now
        return tuple(self.bars)

    def completed_daily_bars(
        self, symbol: str, *, now: datetime, limit: int = 30
    ) -> tuple[Candle, ...]:
        del symbol, now, limit
        return ()

    def latest_completed_daily_bar(self, symbol: str, *, now: datetime):
        del symbol, now

    def cluster_id(self, symbol: str) -> str:
        del symbol
        return "UNKNOWN"


def _v2_candidate() -> DailySetupCandidate:
    decision = SetupDecision(
        symbol="005930",
        approved=True,
        setups=(SetupType.PULLBACK, SetupType.FLOW_REVERSAL),
        violations=(),
        missing_checks=(),
        rsi14=Decimal(50),
        ma50=Decimal(9),
        ma200=Decimal(8),
        ma50_distance=Decimal("0.01"),
        flow_stars=1,
        flow_summary=None,
        valuation_tier=ValuationTier.B,
        confidence_multiplier=Decimal(1),
        proposed_confidence_multiplier=Decimal(1),
    )
    return DailySetupCandidate(
        symbol="005930",
        signal_session=datetime(2026, 8, 11, tzinfo=UTC).date(),
        close_price=Decimal(10),
        setup_low=Decimal(9),
        ma50=Decimal(9),
        atr14=Decimal("0.5"),
        decision=decision,
    )


def _minute_bar(timestamp: datetime, *, open_price: str, low_price: str) -> Candle:
    open_value = Decimal(open_price)
    low_value = Decimal(low_price)
    return Candle(
        symbol="005930",
        interval="1m",
        timestamp=timestamp,
        open_price=open_value,
        high_price=open_value + 1,
        low_price=low_value,
        close_price=open_value,
        volume=Decimal(1000),
        currency="KRW",
    )


def _daily_trend(symbol: str, *, rising: bool) -> list[Candle]:
    start = datetime(2026, 5, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(60):
        price = Decimal(100 + index) if rising else Decimal(200 - index)
        candles.append(
            Candle(
                symbol=symbol,
                interval="1d",
                timestamp=start + timedelta(days=index),
                open_price=price,
                high_price=price,
                low_price=price,
                close_price=price,
                volume=Decimal(1000),
                currency="KRW",
            )
        )
    return candles


class PaperCycleRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.market_repository = SqliteMarketRepository(":memory:")
        self.paper_ledger = PaperLedger(":memory:")
        self.cycle_state = SqliteCycleStateStore(":memory:")

    def tearDown(self) -> None:
        self.market_repository.close()
        self.paper_ledger.close()
        self.cycle_state.close()

    def _runner(
        self,
        client: WatchlistCandleClient,
        *,
        entry_gate=None,
        v2_strategy=None,
    ) -> PaperCycleRunner:
        return PaperCycleRunner(
            collector=MarketCollector(client=client, repository=self.market_repository),
            strategy=StoredMaStrategy(self.market_repository),
            trading=PaperTradingService(
                ledger=self.paper_ledger,
                risk_manager=RiskManager(RiskLimits()),
            ),
            calendar=MarketCalendarService(client),
            performance=PortfolioPerformance(
                ledger=self.paper_ledger,
                market_repository=self.market_repository,
            ),
            state=self.cycle_state,
            entry_gate=entry_gate,
            v2_strategy=v2_strategy,
            clock=lambda: datetime(2026, 8, 12, 7, 0, 2, tzinfo=UTC),
        )

    def test_collects_scans_and_paper_executes_watchlist(self) -> None:
        client = WatchlistCandleClient(
            {
                "005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)],
                "AAPL": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)],
            }
        )

        result = self._runner(client).run(
            symbols=("005930", "AAPL"),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.symbol_count, 2)
        self.assertEqual(result.signal_count, 1)
        self.assertEqual(result.fill_count, 1)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.consecutive_api_errors, 0)
        self.assertEqual(result.daily_return_rate, Decimal(0))
        self.assertEqual(client.calls, [("005930", 4), ("AAPL", 4)])
        self.assertEqual(client.calendar_calls, ["KR"])
        self.assertEqual(self.paper_ledger.position_quantity("005930"), Decimal(1))
        decision_items = [item for item in result.items if item.decision is not None]
        self.assertEqual(len(decision_items), 1)
        self.assertIsNotNone(decision_items[0].decision_id)
        latest = self.cycle_state.latest_run()
        assert latest is not None
        self.assertEqual(latest.status, "succeeded")
        self.assertEqual(latest.fill_count, 1)

    def test_empty_price_candidate_universe_is_successful(self) -> None:
        result = self._runner(WatchlistCandleClient({})).run(
            symbols=(),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.symbol_count, 0)
        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(result.insight["idleReason"], "ok")
        latest = self.cycle_state.latest_run()
        assert latest is not None
        self.assertEqual(latest.status, "succeeded")

    def test_setup_v2_gate_blocks_buy_before_risk_and_advisor(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(
            client,
            entry_gate=lambda signal, now: EntryGateDecision(
                approved=False,
                reason="setup-v2:missing:flow-history,event-calendar",
            ),
        ).run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(client.calls, [("005930", 200)])
        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(result.items[0].idle_reason, "setup-v2-block")
        self.assertEqual(
            result.items[0].skip_reason,
            "setup-v2:missing:flow-history,event-calendar",
        )
        self.assertEqual(result.insight["funnel"]["setupV2Blocked"], 1)
        self.assertEqual(self.paper_ledger.recent_risk_decisions(), [])

    def test_records_no_crossover_when_watchlist_has_no_signal(self) -> None:
        client = WatchlistCandleClient(
            {"AAPL": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        result = self._runner(client).run(
            symbols=("AAPL",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.items[0].idle_reason, "no-crossover")
        self.assertEqual(result.items[0].ma_relation, "above")
        self.assertEqual(result.items[0].close_price, Decimal(13))
        self.assertEqual(result.items[0].short_ma, Decimal("12.5"))
        self.assertEqual(result.items[0].long_ma, Decimal(12))
        insight = result.insight
        self.assertEqual(insight["idleReason"], "no-crossover")
        self.assertEqual(insight["funnel"]["scanned"], 1)
        self.assertEqual(insight["funnel"]["noCrossover"], 1)
        self.assertEqual(insight["reasons"], {"no-crossover": 1})
        self.assertEqual(insight["symbols"][0]["symbol"], "AAPL")
        self.assertEqual(insight["symbols"][0]["relation"], "above")
        latest = self.cycle_state.latest_run()
        assert latest is not None
        assert latest.cycle_insight is not None
        self.assertEqual(json.loads(latest.cycle_insight)["idleReason"], "no-crossover")

    def test_1m_continuation_buys_when_daily_trend_is_risk_on(self) -> None:
        self.market_repository.upsert_candles(_daily_trend("005930", rising=True))
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        result = self._runner(client).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.signal_count, 1)
        self.assertEqual(result.fill_count, 1)
        assert result.items[0].signal is not None
        self.assertEqual(result.items[0].signal.reason, "MA2/MA3 trend continuation")
        self.assertTrue(result.items[0].signal.signal_id.endswith("cont-2026-08-12"))

    def test_v2_cycle_arms_daily_candidate_and_persists_sized_plan(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=5),
        )

        self.assertEqual(result.fill_count, 1)
        assert result.items[0].fill is not None
        self.assertEqual(result.items[0].fill.side, Side.BUY)
        self.assertGreater(result.items[0].fill.quantity, Decimal(1))
        plan = self.paper_ledger.v2_position_plan("005930")
        assert plan is not None
        self.assertEqual(plan.quantity, result.items[0].fill.quantity)
        self.assertEqual(plan.cluster_id, "UNKNOWN")

    def test_v2_cycle_allows_entry_through_thirty_minute_arm_window(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=30),
        )

        self.assertEqual(result.fill_count, 1)
        self.assertIsNotNone(self.paper_ledger.v2_position_plan("005930"))

    def test_v2_cycle_allows_entire_thirtieth_minute(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=30, seconds=59),
        )

        self.assertEqual(result.fill_count, 1)
        self.assertIsNotNone(self.paper_ledger.v2_position_plan("005930"))

    def test_v2_cycle_records_armable_late_entry_as_shadow_only(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=31),
        )

        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(
            result.items[0].skip_reason,
            "setup-v2:shadow:armed-after-entry-window",
        )
        self.assertEqual(result.items[0].idle_reason, "shadow-signal")
        self.assertEqual(result.insight["funnel"]["shadowSignals"], 1)
        self.assertIsNone(self.paper_ledger.v2_position_plan("005930"))
        self.assertEqual(self.paper_ledger.recent_risk_decisions(), [])

    def test_v2_late_shadow_preserves_deterministic_gate_rejection(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="11",
                    low_price="10",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=31),
        )

        self.assertEqual(result.fill_count, 0)
        self.assertEqual(
            result.items[0].skip_reason,
            "setup-v2:violation:gap-up-chase",
        )
        self.assertEqual(result.insight["funnel"]["shadowSignals"], 0)

    def test_v2_rejection_is_not_hidden_by_missing_opening_bar(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        candidate = _v2_candidate()
        candidate = replace(
            candidate,
            decision=replace(
                candidate.decision,
                approved=False,
                violations=("missing-price-setup",),
            ),
        )
        strategy = FakeV2CycleStrategy(candidate, [])
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open,
        )

        self.assertEqual(result.fill_count, 0)
        self.assertEqual(
            result.items[0].skip_reason,
            "setup-v2:violation:missing-price-setup",
        )
        self.assertNotIn(
            ("005930", "1m", 200),
            client.interval_calls,
        )

    def test_hermes_experimental_can_review_price_strategy_rejection(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        candidate = _v2_candidate()
        candidate = replace(
            candidate,
            close_price=Decimal(100000),
            setup_low=Decimal(90000),
            atr14=Decimal(10000),
            decision=replace(
                candidate.decision,
                approved=False,
                setups=(),
                violations=("missing-price-setup", "rsi-chase"),
            ),
        )
        strategy = FakeV2CycleStrategy(
            candidate,
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="100000",
                    low_price="95000",
                )
            ],
        )

        result = self._runner(
            WatchlistCandleClient(
                {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
            ),
            v2_strategy=strategy,
        ).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=5),
            signal_namespace="hermes",
            experimental_strategy_reference=True,
        )

        self.assertEqual(result.fill_count, 1)
        assert result.items[0].signal is not None
        self.assertTrue(
            result.items[0].signal.signal_id.startswith(
                "hermes:hermes-experimental-v2.3:"
            )
        )
        self.assertIn("missing-price-setup", result.items[0].signal.reason)
        self.assertIn("rsi-chase", result.items[0].signal.reason)
        plan = self.paper_ledger.v2_position_plan("005930")
        assert plan is not None
        self.assertIn("hermes-experimental-reference", plan.setups)
        self.assertGreater(plan.planned_heat, Decimal(5000))
        self.assertLessEqual(plan.planned_heat, Decimal(20000))

    def test_hermes_experimental_keeps_missing_data_and_event_hard(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        base = _v2_candidate()
        cases = (
            replace(
                base,
                decision=replace(
                    base.decision,
                    approved=False,
                    missing_checks=("flow-history",),
                ),
            ),
            replace(
                base,
                decision=replace(
                    base.decision,
                    approved=False,
                    violations=("event-imminent",),
                ),
            ),
            replace(
                base,
                decision=replace(
                    base.decision,
                    approved=False,
                    violations=("future-unknown-gate",),
                ),
            ),
        )

        for candidate in cases:
            with self.subTest(candidate=candidate.decision):
                ledger = PaperLedger(":memory:", portfolio_id="hermes")
                state = SqliteCycleStateStore(":memory:", portfolio_id="hermes")
                try:
                    strategy = FakeV2CycleStrategy(
                        candidate,
                        [
                            _minute_bar(
                                market_open + timedelta(minutes=1),
                                open_price="10",
                                low_price="9.5",
                            )
                        ],
                    )
                    runner = PaperCycleRunner(
                        collector=MarketCollector(
                            client=WatchlistCandleClient(
                                {
                                    "005930": [
                                        Decimal(10),
                                        Decimal(10),
                                        Decimal(10),
                                        Decimal(12),
                                    ]
                                }
                            ),
                            repository=self.market_repository,
                        ),
                        strategy=StoredMaStrategy(self.market_repository),
                        trading=PaperTradingService(
                            ledger=ledger,
                            risk_manager=RiskManager(RiskLimits()),
                        ),
                        calendar=MarketCalendarService(
                            WatchlistCandleClient({"005930": [Decimal(10)]})
                        ),
                        performance=PortfolioPerformance(
                            ledger=ledger,
                            market_repository=self.market_repository,
                        ),
                        state=state,
                        v2_strategy=strategy,
                    )
                    result = runner.run(
                        symbols=("005930",),
                        interval="1m",
                        short_window=2,
                        long_window=3,
                        quantity=Decimal(1),
                        now=market_open + timedelta(minutes=5),
                        signal_namespace="hermes",
                        experimental_strategy_reference=True,
                    )
                    self.assertEqual(result.fill_count, 0)
                    assert result.items[0].skip_reason is not None
                    self.assertTrue(result.items[0].skip_reason.startswith("setup-v2:"))
                finally:
                    ledger.close()
                    state.close()

    def test_v2_cycle_pages_back_to_toss_session_open_bar(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        opening_bar = _minute_bar(
            market_open + timedelta(minutes=1),
            open_price="10",
            low_price="9.5",
        )
        strategy = FakeV2CycleStrategy(_v2_candidate(), [])

        class PagingMinuteClient(WatchlistCandleClient):
            def candles(
                self,
                symbol: str,
                *,
                interval: str,
                count: int,
                before: str | None,
                adjusted: bool,
            ) -> dict:
                if interval != "1m":
                    return super().candles(
                        symbol,
                        interval=interval,
                        count=count,
                        before=before,
                        adjusted=adjusted,
                    )
                self.calls.append((symbol, count))
                self.interval_calls.append((symbol, interval, count))
                self.before_calls.append((symbol, interval, before))
                if before is None:
                    timestamp = market_open + timedelta(hours=3)
                    next_before = "older-minute-page"
                    strategy.bars.append(
                        _minute_bar(
                            timestamp,
                            open_price="10",
                            low_price="9.5",
                        )
                    )
                else:
                    timestamp = opening_bar.timestamp
                    next_before = None
                    strategy.bars.append(opening_bar)
                return {
                    "candles": [
                        {
                            "timestamp": timestamp.isoformat(),
                            "openPrice": "10",
                            "highPrice": "10.5",
                            "lowPrice": "9.5",
                            "closePrice": "10",
                            "volume": "100",
                            "currency": "KRW",
                        }
                    ],
                    "nextBefore": next_before,
                }

        client = PagingMinuteClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=market_open + timedelta(minutes=5),
        )

        self.assertEqual(result.fill_count, 1)
        self.assertIn(
            ("005930", "1m", "older-minute-page"),
            client.before_calls,
        )

    def test_v2_cycle_ignores_ma_sell_and_exits_after_stop_touch(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(12), Decimal(13), Decimal(10)]}
        )
        runner = self._runner(client, v2_strategy=strategy)
        runner.run(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=market_open + timedelta(minutes=5),
        )
        strategy.bars = [
            _minute_bar(
                market_open + timedelta(minutes=1),
                open_price="10",
                low_price="9.5",
            ),
            _minute_bar(
                market_open + timedelta(minutes=2),
                open_price="9.4",
                low_price="8.9",
            ),
            _minute_bar(
                market_open + timedelta(minutes=3),
                open_price="8.8",
                low_price="8.7",
            ),
        ]

        result = runner.run(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=datetime(2026, 8, 12, 7, 5, tzinfo=UTC),
        )

        self.assertEqual(result.fill_count, 1)
        assert result.items[0].fill is not None
        self.assertEqual(result.items[0].fill.side, Side.SELL)
        self.assertEqual(
            result.items[0].fill.price,
            Decimal("8.8") * Decimal("0.9995"),
        )
        self.assertIsNone(self.paper_ledger.v2_position_plan("005930"))

    def test_v2_held_plan_without_exit_is_v2_idle(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )
        runner = self._runner(client, v2_strategy=strategy)
        runner.run(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=market_open + timedelta(minutes=5),
        )

        result = runner.run(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=datetime(2026, 8, 12, 7, 5, tzinfo=UTC),
        )

        self.assertEqual(result.fill_count, 0)
        self.assertEqual(result.items[0].idle_reason, "v2-idle")
        self.assertEqual(result.insight["funnel"]["v2Idle"], 1)

    def test_v2_rebuilds_candidate_for_shared_snapshot_portfolio(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [
                _minute_bar(
                    market_open + timedelta(minutes=1),
                    open_price="10",
                    low_price="9.5",
                )
            ],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )
        rule_runner = self._runner(client, v2_strategy=strategy)
        now = market_open + timedelta(minutes=5)
        snapshot = rule_runner.prepare(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=now,
        )
        serialized_snapshot = replace(snapshot, v2_candidates=())
        hermes_ledger = PaperLedger(":memory:", portfolio_id="hermes")
        hermes_state = SqliteCycleStateStore(":memory:", portfolio_id="hermes")
        try:
            hermes_runner = PaperCycleRunner(
                collector=MarketCollector(
                    client=client, repository=self.market_repository
                ),
                strategy=StoredMaStrategy(self.market_repository),
                trading=PaperTradingService(
                    ledger=hermes_ledger,
                    risk_manager=RiskManager(RiskLimits()),
                ),
                calendar=MarketCalendarService(client),
                performance=PortfolioPerformance(
                    ledger=hermes_ledger,
                    market_repository=self.market_repository,
                ),
                state=hermes_state,
                v2_strategy=strategy,
                clock=lambda: now + timedelta(seconds=2),
            )

            result = hermes_runner.run(
                symbols=("005930",), interval="1m", short_window=2, long_window=3,
                quantity=Decimal(1), now=now, snapshot=serialized_snapshot,
            )

            self.assertEqual(result.fill_count, 1)
            self.assertIsNotNone(hermes_ledger.v2_position_plan("005930"))
        finally:
            hermes_ledger.close()
            hermes_state.close()

    def test_v2_quarantines_legacy_positions_without_failing_cycle(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [_minute_bar(market_open, open_price="10", low_price="9.5")],
        )
        self.paper_ledger.execute(
            TradeSignal(
                signal_id="legacy-position",
                symbol="005930",
                side=Side.BUY,
                reference_price=Decimal(10),
                quantity=Decimal(1),
                reason="legacy MA",
            ),
            executed_at=market_open,
        )
        client = WatchlistCandleClient(
            {
                "005930": [Decimal(10), Decimal(12), Decimal(13), Decimal(10)],
                "000660": [Decimal(10), Decimal(12), Decimal(13), Decimal(10)],
            }
        )

        result = self._runner(client, v2_strategy=strategy).run(
            symbols=("005930", "000660"), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(
            result.items[0].skip_reason,
            "setup-v2:blocked:legacy-position-unmanaged",
        )
        self.assertEqual(
            result.items[1].skip_reason,
            "setup-v2:blocked:legacy-portfolio",
        )
        self.assertEqual(result.insight["funnel"]["setupV2Blocked"], 2)

    def test_v2_shared_snapshot_does_not_rebuild_candidate_for_legacy(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [_minute_bar(market_open, open_price="10", low_price="9.5")],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(12), Decimal(13), Decimal(10)]}
        )
        now = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
        snapshot = self._runner(client, v2_strategy=strategy).prepare(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=now,
        )
        hermes_ledger = PaperLedger(":memory:", portfolio_id="hermes")
        hermes_state = SqliteCycleStateStore(":memory:", portfolio_id="hermes")
        try:
            hermes_ledger.execute(
                TradeSignal(
                    signal_id="legacy-hermes-position",
                    symbol="005930",
                    side=Side.BUY,
                    reference_price=Decimal(10),
                    quantity=Decimal(1),
                    reason="legacy MA",
                ),
                executed_at=market_open,
            )
            strategy.build_error = ValueError(
                "setup-v2:missing:completed-daily-candles(63/200)"
            )
            runner = PaperCycleRunner(
                collector=MarketCollector(
                    client=client, repository=self.market_repository
                ),
                strategy=StoredMaStrategy(self.market_repository),
                trading=PaperTradingService(
                    ledger=hermes_ledger,
                    risk_manager=RiskManager(RiskLimits()),
                ),
                calendar=MarketCalendarService(client),
                performance=PortfolioPerformance(
                    ledger=hermes_ledger,
                    market_repository=self.market_repository,
                ),
                state=hermes_state,
                v2_strategy=strategy,
                clock=lambda: now + timedelta(seconds=2),
            )

            result = runner.run(
                symbols=("005930",), interval="1m", short_window=2, long_window=3,
                quantity=Decimal(1), now=now,
                snapshot=replace(snapshot, v2_candidates=()),
            )

            self.assertEqual(result.failed_count, 0)
            self.assertEqual(
                result.items[0].skip_reason,
                "setup-v2:blocked:legacy-position-unmanaged",
            )
        finally:
            hermes_ledger.close()
            hermes_state.close()

    def test_hermes_shared_snapshot_skips_missing_daily_candles(self) -> None:
        market_open = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [_minute_bar(market_open, open_price="10", low_price="9.5")],
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(12), Decimal(13), Decimal(10)]}
        )
        now = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
        snapshot = self._runner(client, v2_strategy=strategy).prepare(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=now,
        )
        strategy.build_error = ValueError(
            "setup-v2:missing:completed-daily-candles(60/200)"
        )
        hermes_ledger = PaperLedger(":memory:", portfolio_id="hermes")
        hermes_state = SqliteCycleStateStore(":memory:", portfolio_id="hermes")
        try:
            result = PaperCycleRunner(
                collector=MarketCollector(
                    client=client, repository=self.market_repository
                ),
                strategy=StoredMaStrategy(self.market_repository),
                trading=PaperTradingService(
                    ledger=hermes_ledger,
                    risk_manager=RiskManager(RiskLimits()),
                ),
                calendar=MarketCalendarService(client),
                performance=PortfolioPerformance(
                    ledger=hermes_ledger,
                    market_repository=self.market_repository,
                ),
                state=hermes_state,
                v2_strategy=strategy,
                clock=lambda: now + timedelta(seconds=2),
            ).run(
                symbols=("005930",), interval="1m", short_window=2, long_window=3,
                quantity=Decimal(1), now=now,
                snapshot=replace(snapshot, v2_candidates=()),
            )

            self.assertEqual(result.failed_count, 0)
            self.assertEqual(result.skipped_count, 1)
            self.assertEqual(
                result.items[0].skip_reason,
                "setup-v2:missing:completed-daily-candles(60/200)",
            )
            self.assertIsNone(result.items[0].error)
            self.assertEqual(result.insight["funnel"]["setupV2Blocked"], 1)
        finally:
            hermes_ledger.close()
            hermes_state.close()

    def test_1m_v2_prepare_collects_completed_daily_history(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]},
            daily_next_before="older-cursor",
        )
        strategy = FakeV2CycleStrategy(
            _v2_candidate(),
            [_minute_bar(datetime(2026, 8, 12, 0, 0, tzinfo=UTC), open_price="10", low_price="9.5")],
        )

        self._runner(client, v2_strategy=strategy).prepare(
            symbols=("005930",), interval="1m", short_window=2, long_window=3,
            quantity=Decimal(1), now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertIn(("005930", "1m", 4), client.interval_calls)
        self.assertIn(("005930", "1d", 200), client.interval_calls)
        self.assertIn(("005930", "1d", 1), client.interval_calls)
        self.assertIn(
            ("005930", "1d", "older-cursor"),
            client.before_calls,
        )

    def test_1m_continuation_skips_when_daily_trend_is_not_risk_on(self) -> None:
        self.market_repository.upsert_candles(_daily_trend("005930", rising=False))
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        result = self._runner(client).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.items[0].idle_reason, "no-crossover")

    def test_1m_continuation_skips_held_symbol(self) -> None:
        self.market_repository.upsert_candles(_daily_trend("005930", rising=True))
        self.paper_ledger.execute(
            TradeSignal(
                signal_id="held-005930",
                symbol="005930",
                side=Side.BUY,
                reference_price=Decimal(10),
                quantity=Decimal(1),
                reason="bootstrap",
            ),
            executed_at=datetime(2026, 8, 11, tzinfo=UTC),
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        result = self._runner(client).run(
            symbols=("005930",),
            interval="1m",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(result.items[0].idle_reason, "already-held")

    def test_records_sell_without_position_instead_of_silent_drop(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(12), Decimal(13), Decimal(10)]}
        )

        def buy_only_gate(signal, now):
            raise AssertionError("setup-v2 entry gate must not inspect SELL")

        result = self._runner(client, entry_gate=buy_only_gate).run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.items[0].idle_reason, "sell-no-position")
        self.assertEqual(result.insight["idleReason"], "sell-no-position")
        self.assertEqual(result.insight["funnel"]["sellNoPosition"], 1)

    def test_reuses_one_market_snapshot_across_portfolios(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )
        now = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
        rule_runner = self._runner(client)
        snapshot = rule_runner.prepare(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=now,
        )
        rule_result = rule_runner.run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=now,
            signal_namespace="rule",
            snapshot=snapshot,
        )

        hermes_ledger = PaperLedger(":memory:", portfolio_id="hermes")
        hermes_state = SqliteCycleStateStore(":memory:", portfolio_id="hermes")
        try:
            hermes_runner = PaperCycleRunner(
                collector=MarketCollector(
                    client=client, repository=self.market_repository
                ),
                strategy=StoredMaStrategy(self.market_repository),
                trading=PaperTradingService(
                    ledger=hermes_ledger,
                    risk_manager=RiskManager(RiskLimits()),
                ),
                calendar=MarketCalendarService(client),
                performance=PortfolioPerformance(
                    ledger=hermes_ledger,
                    market_repository=self.market_repository,
                ),
                state=hermes_state,
                clock=lambda: datetime(2026, 8, 12, 7, 0, 3, tzinfo=UTC),
            )
            hermes_result = hermes_runner.run(
                symbols=("005930",),
                interval="1d",
                short_window=2,
                long_window=3,
                quantity=Decimal(1),
                now=now,
                signal_namespace="hermes",
                snapshot=snapshot,
            )
        finally:
            hermes_ledger.close()
            hermes_state.close()

        self.assertEqual(client.calls, [("005930", 4)])
        self.assertEqual(rule_result.fill_count, 1)
        self.assertEqual(hermes_result.fill_count, 1)
        assert rule_result.items[0].signal is not None
        assert hermes_result.items[0].signal is not None
        self.assertTrue(rule_result.items[0].signal.signal_id.startswith("rule:"))
        self.assertTrue(
            hermes_result.items[0].signal.signal_id.startswith("hermes:")
        )

    def test_enters_existing_uptrend_only_on_universe_refresh(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        result = self._runner(client).run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
            trend_entry_symbols=("005930",),
            trend_entry_key="universe-run-1",
        )

        self.assertEqual(result.signal_count, 1)
        self.assertEqual(result.fill_count, 1)
        assert result.items[0].signal is not None
        self.assertEqual(result.items[0].signal.reason, "MA2/MA3 trend entry")

    def test_ignores_sell_signal_without_position(self) -> None:
        client = WatchlistCandleClient(
            {"035420": [Decimal(10), Decimal(12), Decimal(12), Decimal(5)]}
        )

        result = self._runner(client).run(
            symbols=("035420",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.signal_count, 0)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(client.calendar_calls, [])
        self.assertEqual(self.paper_ledger.recent_risk_decisions(), [])

    def test_isolates_symbol_failure_and_continues(self) -> None:
        client = WatchlistCandleClient(
            {"AAPL": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        result = self._runner(client).run(
            symbols=("BAD", "AAPL"),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.items[0].symbol, "BAD")
        self.assertEqual(result.items[0].error, "simulated-symbol-error")
        self.assertIsNone(result.items[1].error)
        self.assertEqual(result.consecutive_api_errors, 1)
        self.assertEqual(
            self.cycle_state.latest_consecutive_api_errors(),
            1,
        )

        second = self._runner(client).run(
            symbols=("BAD", "AAPL"),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 1, tzinfo=UTC),
        )

        self.assertEqual(second.consecutive_api_errors, 2)

    def test_skips_symbol_with_insufficient_ma_history(self) -> None:
        client = WatchlistCandleClient({"487400": [Decimal(10)]})

        result = self._runner(client).run(
            symbols=("487400",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.consecutive_api_errors, 0)
        self.assertEqual(result.items[0].skip_reason, "need 4 candles, found 1")
        self.assertEqual(result.items[0].idle_reason, "insufficient-candles")
        self.assertIsNone(result.items[0].error)
        latest = self.cycle_state.latest_run()
        assert latest is not None
        self.assertEqual(latest.status, "succeeded")
        assert latest.cycle_insight is not None
        self.assertEqual(json.loads(latest.cycle_insight)["idleReason"], "insufficient-candles")

    def test_daily_loss_is_calculated_and_passed_to_risk_manager(self) -> None:
        self.paper_ledger.execute(
            TradeSignal(
                signal_id="bootstrap-000660",
                symbol="000660",
                side=Side.BUY,
                reference_price=Decimal(1000000),
                quantity=Decimal(1),
                reason="bootstrap",
            ),
            executed_at=datetime(2026, 8, 11, tzinfo=UTC),
        )
        client = WatchlistCandleClient(
            {
                "005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)],
                "000660": [Decimal(1000000), Decimal(950000)],
            }
        )

        result = self._runner(client).run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(
            result.daily_return_rate,
            Decimal(949850) / Decimal(999850) - Decimal(1),
        )
        self.assertEqual(client.calls, [("005930", 4), ("000660", 2)])
        self.assertIsNotNone(result.items[0].decision)
        assert result.items[0].decision is not None
        self.assertIn("daily-loss-limit", result.items[0].decision.violations)
        self.assertEqual(result.fill_count, 0)

    def test_holiday_calendar_blocks_new_buy_without_cycle_failure(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]},
            closed_countries=frozenset({"KR"}),
        )

        result = self._runner(client).run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.fill_count, 0)
        assert result.items[0].decision is not None
        self.assertIn("market-closed", result.items[0].decision.violations)
        self.assertEqual(result.items[0].idle_reason, "risk-block")

    def test_partial_api_failure_feeds_persisted_streak_to_other_signal(self) -> None:
        run_id = self.cycle_state.start_run(
            started_at=datetime(2026, 8, 12, 6, 0, tzinfo=UTC),
            interval="1d",
            symbol_count=1,
        )
        self.cycle_state.finish_run(
            run_id=run_id,
            finished_at=datetime(2026, 8, 12, 6, 0, 1, tzinfo=UTC),
            status="failed",
            signal_count=0,
            fill_count=0,
            failed_count=1,
            consecutive_api_errors=4,
            daily_return_rate=Decimal(0),
            error_message="prior outage",
        )
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        result = self._runner(client).run(
            symbols=("BAD", "005930"),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(result.consecutive_api_errors, 5)
        self.assertEqual(result.fill_count, 0)
        assert result.items[1].decision is not None
        self.assertIn("api-error-kill-switch", result.items[1].decision.violations)

    def test_shared_snapshot_json_keeps_ma_states(self) -> None:
        from toss_trader.cli import _cycle_snapshot_to_dict, _read_cycle_snapshot

        client = WatchlistCandleClient(
            {"AAPL": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )
        snapshot = self._runner(client).prepare(
            symbols=("AAPL",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )
        payload = _cycle_snapshot_to_dict(
            snapshot, symbol_names={"AAPL": "Apple Inc."}
        )
        self.assertEqual(
            payload["instruments"], [{"symbol": "AAPL", "name": "Apple Inc."}]
        )
        payload["evaluatedAt"] = snapshot.evaluated_at.isoformat()
        stdin = json.dumps(payload)

        with patch("toss_trader.cli.sys.stdin", io.StringIO(stdin)):
            restored = _read_cycle_snapshot()

        self.assertEqual(restored.ma_states[0].relation, "above")
        self.assertEqual(restored.ma_states[0].close, Decimal(13))
        self.assertIsNone(restored.signals[0])


if __name__ == "__main__":
    unittest.main()
