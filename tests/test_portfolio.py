import unittest
from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.models import Candle, Side, TradeSignal
from toss_trader.paper import PaperLedger
from toss_trader.portfolio import PortfolioPerformance
from toss_trader.repository import SqliteMarketRepository


def candle(
    symbol: str,
    close: Decimal,
    *,
    day: int,
    currency: str,
    interval: str = "1d",
) -> Candle:
    return Candle(
        symbol=symbol,
        interval=interval,
        timestamp=datetime(2026, 8, day, tzinfo=UTC),
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        volume=Decimal(100),
        currency=currency,
    )


class PortfolioPerformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = PaperLedger(":memory:")
        self.market = SqliteMarketRepository(":memory:")

    def tearDown(self) -> None:
        self.ledger.close()
        self.market.close()

    def _buy(self, symbol: str, price: Decimal, quantity: Decimal) -> None:
        self.ledger.execute(
            TradeSignal(
                signal_id=f"buy-{symbol}",
                symbol=symbol,
                side=Side.BUY,
                reference_price=price,
                quantity=quantity,
                reason="bootstrap",
            ),
            executed_at=datetime(2026, 8, 11, tzinfo=UTC),
        )

    def test_returns_zero_without_open_positions(self) -> None:
        result = PortfolioPerformance(
            ledger=self.ledger, market_repository=self.market
        ).daily()

        self.assertEqual(result.daily_return_rate, Decimal(0))
        self.assertEqual(result.currency_returns, {})

    def test_calculates_weighted_daily_return_within_currency(self) -> None:
        self._buy("005930", Decimal(100), Decimal(2))
        self._buy("000660", Decimal(200), Decimal(1))
        self.market.upsert_candles(
            [
                candle("005930", Decimal(100), day=11, currency="KRW"),
                candle("005930", Decimal(90), day=12, currency="KRW"),
                candle("000660", Decimal(200), day=11, currency="KRW"),
                candle("000660", Decimal(220), day=12, currency="KRW"),
            ]
        )

        result = PortfolioPerformance(
            ledger=self.ledger, market_repository=self.market
        ).daily()

        self.assertEqual(result.currency_returns, {"KRW": Decimal(0)})
        self.assertEqual(result.daily_return_rate, Decimal(0))

    def test_uses_worst_currency_return_instead_of_summing_currencies(self) -> None:
        self._buy("005930", Decimal(100), Decimal(1))
        self._buy("AAPL", Decimal(100), Decimal(1))
        self.market.upsert_candles(
            [
                candle("005930", Decimal(100), day=11, currency="KRW"),
                candle("005930", Decimal(99), day=12, currency="KRW"),
                candle("AAPL", Decimal(100), day=11, currency="USD"),
                candle("AAPL", Decimal(96), day=12, currency="USD"),
            ]
        )

        result = PortfolioPerformance(
            ledger=self.ledger, market_repository=self.market
        ).daily()

        self.assertEqual(result.currency_returns["KRW"], Decimal("-0.01"))
        self.assertEqual(result.currency_returns["USD"], Decimal("-0.04"))
        self.assertEqual(result.daily_return_rate, Decimal("-0.04"))

    def test_uses_fill_cost_and_latest_minute_for_new_position(self) -> None:
        self._buy("005930", Decimal(100), Decimal(1))
        self.market.upsert_candles(
            [
                candle("005930", Decimal(100), day=12, currency="KRW"),
                candle(
                    "005930",
                    Decimal(97),
                    day=13,
                    currency="KRW",
                    interval="1m",
                ),
            ]
        )

        result = PortfolioPerformance(
            ledger=self.ledger, market_repository=self.market
        ).daily()

        self.assertEqual(result.currency_returns, {"KRW": Decimal("-0.03")})
        self.assertEqual(result.daily_return_rate, Decimal("-0.000003"))

    def test_values_new_intraday_position_without_daily_candle(self) -> None:
        self._buy("005930", Decimal(100000), Decimal(1))
        self.market.upsert_candles(
            [
                candle(
                    "005930",
                    Decimal(101000),
                    day=13,
                    currency="KRW",
                    interval="1m",
                )
            ]
        )

        result = PortfolioPerformance(
            ledger=self.ledger, market_repository=self.market
        ).daily(now=datetime(2026, 8, 13, 7, 0, tzinfo=UTC))

        self.assertEqual(result.equity, Decimal(1000985))
        self.assertEqual(result.unrealized_pnl, Decimal(985))

    def test_reports_fee_adjusted_equity_and_net_pnl(self) -> None:
        self._buy("005930", Decimal(100000), Decimal(1))
        self.market.upsert_candles(
            [
                candle("005930", Decimal(100000), day=11, currency="KRW"),
                candle("005930", Decimal(110000), day=12, currency="KRW"),
            ]
        )

        result = PortfolioPerformance(
            ledger=self.ledger,
            market_repository=self.market,
            initial_cash=Decimal(1000000),
        ).daily(now=datetime(2026, 8, 12, 7, 0, tzinfo=UTC))

        self.assertEqual(result.equity, Decimal(1009985))
        self.assertEqual(result.realized_pnl, Decimal(0))
        self.assertEqual(result.unrealized_pnl, Decimal(9985))
        self.assertEqual(result.total_costs, Decimal(15))

    def test_uses_first_snapshot_as_intraday_return_baseline(self) -> None:
        self._buy("005930", Decimal(100000), Decimal(1))
        self.market.upsert_candles(
            [
                candle("005930", Decimal(100000), day=11, currency="KRW"),
                candle("005930", Decimal(110000), day=12, currency="KRW"),
            ]
        )
        performance = PortfolioPerformance(
            ledger=self.ledger,
            market_repository=self.market,
            initial_cash=Decimal(1000000),
        )

        first = performance.daily(now=datetime(2026, 8, 12, 1, 0, tzinfo=UTC))
        self.market.upsert_candles(
            [candle("005930", Decimal(99000), day=12, currency="KRW")]
        )
        second = performance.daily(now=datetime(2026, 8, 12, 2, 0, tzinfo=UTC))

        self.assertEqual(
            first.daily_return_rate, Decimal(1009985) / Decimal(999985) - 1
        )
        self.assertEqual(
            second.daily_return_rate, Decimal(998985) / Decimal(999985) - 1
        )


if __name__ == "__main__":
    unittest.main()
