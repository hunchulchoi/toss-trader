import unittest
from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.models import Candle, Side, TradeSignal
from toss_trader.paper import PaperLedger
from toss_trader.portfolio import PortfolioPerformance
from toss_trader.repository import SqliteMarketRepository


def candle(symbol: str, close: Decimal, *, day: int, currency: str) -> Candle:
    return Candle(
        symbol=symbol,
        interval="1d",
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

    def test_requires_two_daily_marks_for_each_open_position(self) -> None:
        self._buy("005930", Decimal(100), Decimal(1))
        self.market.upsert_candles(
            [candle("005930", Decimal(100), day=12, currency="KRW")]
        )

        with self.assertRaisesRegex(ValueError, "two daily candles"):
            PortfolioPerformance(
                ledger=self.ledger, market_repository=self.market
            ).daily()


if __name__ == "__main__":
    unittest.main()
