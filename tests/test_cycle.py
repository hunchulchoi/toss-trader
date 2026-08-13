import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.calendar import MarketCalendarService
from toss_trader.cycle import PaperCycleRunner
from toss_trader.cycle_state import SqliteCycleStateStore
from toss_trader.execution import PaperTradingService
from toss_trader.market_data import MarketCollector, StoredMaStrategy
from toss_trader.models import Side, TradeSignal
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


class PaperCycleRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.market_repository = SqliteMarketRepository(":memory:")
        self.paper_ledger = PaperLedger(":memory:")
        self.cycle_state = SqliteCycleStateStore(":memory:")

    def tearDown(self) -> None:
        self.market_repository.close()
        self.paper_ledger.close()
        self.cycle_state.close()

    def _runner(self, client: WatchlistCandleClient) -> PaperCycleRunner:
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

    def test_daily_loss_is_calculated_and_passed_to_risk_manager(self) -> None:
        self.paper_ledger.execute(
            TradeSignal(
                signal_id="bootstrap-000660",
                symbol="000660",
                side=Side.BUY,
                reference_price=Decimal(100),
                quantity=Decimal(1),
                reason="bootstrap",
            ),
            executed_at=datetime(2026, 8, 11, tzinfo=UTC),
        )
        client = WatchlistCandleClient(
            {
                "005930": [Decimal(10), Decimal(10), Decimal(10), Decimal(12)],
                "000660": [Decimal(100), Decimal(95)],
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

        self.assertEqual(result.daily_return_rate, Decimal("-0.05"))
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


if __name__ == "__main__":
    unittest.main()
