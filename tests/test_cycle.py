import io
import json
import unittest
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


class WatchlistCandleClient:
    def __init__(
        self,
        closes: dict[str, list[Decimal]],
        *,
        closed_countries: frozenset[str] = frozenset(),
    ) -> None:
        self.closes = closes
        self.calls: list[tuple[str, int]] = []
        self.calendar_calls: list[str] = []
        self.closed_countries = closed_countries

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
        del before, adjusted
        self.calls.append((symbol, count))
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
            "nextBefore": None,
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
        market_context: object | None = None,
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
            clock=lambda: datetime(2026, 8, 12, 7, 0, 2, tzinfo=UTC),
            market_context=market_context,  # type: ignore[arg-type]
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

    def test_signal_collects_market_context_and_blocks_warned_buy(self) -> None:
        client = WatchlistCandleClient(
            {"005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)]}
        )

        class Collector:
            def __init__(self) -> None:
                self.symbols: list[str] = []

            def collect(self, symbol: str):  # type: ignore[no-untyped-def]
                from toss_trader.market_context import MarketContext

                self.symbols.append(symbol)
                return MarketContext(
                    symbol=symbol,
                    payload={"warnings": ["INVESTMENT_WARNING"]},
                    errors=(),
                )

        collector = Collector()
        result = self._runner(client, market_context=collector).run(
            symbols=("005930",),
            interval="1d",
            short_window=2,
            long_window=3,
            quantity=Decimal(1),
            now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        )

        self.assertEqual(collector.symbols, ["005930"])
        self.assertEqual(result.signal_count, 1)
        self.assertEqual(result.fill_count, 0)
        self.assertEqual(result.items[0].idle_reason, "risk-block")
        self.assertEqual(result.insight["symbols"][0]["warnings"], ["INVESTMENT_WARNING"])

    def test_records_no_crossover_when_watchlist_has_no_signal(self) -> None:
        client = WatchlistCandleClient(
            {"AAPL": [Decimal(10), Decimal(11), Decimal(12), Decimal(13)]}
        )

        class Collector:
            def __init__(self) -> None:
                self.symbols: list[str] = []

            def collect(self, symbol: str):  # type: ignore[no-untyped-def]
                self.symbols.append(symbol)
                raise AssertionError("no-signal cycle must not fetch market context")

        result = self._runner(client, market_context=Collector()).run(
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

        result = self._runner(client).run(
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
        payload = _cycle_snapshot_to_dict(snapshot)
        payload["evaluatedAt"] = snapshot.evaluated_at.isoformat()
        stdin = json.dumps(payload)

        with patch("toss_trader.cli.sys.stdin", io.StringIO(stdin)):
            restored = _read_cycle_snapshot()

        self.assertEqual(restored.ma_states[0].relation, "above")
        self.assertEqual(restored.ma_states[0].close, Decimal(13))
        self.assertIsNone(restored.signals[0])


if __name__ == "__main__":
    unittest.main()
