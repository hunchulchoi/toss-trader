import unittest

from toss_trader.errors import TossApiError
from toss_trader.market_context import MarketContextCollector


class FakeContextClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def prices(self, symbols: tuple[str, ...]) -> list[dict]:
        self.calls.append(("prices", ",".join(symbols)))
        return [
            {
                "symbol": symbols[0],
                "lastPrice": "72000",
                "timestamp": "2026-08-14T10:00:00+09:00",
                "currency": "KRW",
            }
        ]

    def orderbook(self, symbol: str) -> dict:
        self.calls.append(("orderbook", symbol))
        return {
            "asks": [{"price": "72100", "volume": "10"}],
            "bids": [{"price": "72000", "volume": "20"}],
            "currency": "KRW",
        }

    def trades(self, symbol: str, *, count: int = 10) -> list[dict]:
        self.calls.append(("trades", symbol))
        del count
        return [{"price": "72000", "volume": "5", "timestamp": "2026-08-14T10:00:00+09:00"}]

    def price_limits(self, symbol: str) -> dict:
        self.calls.append(("price_limits", symbol))
        return {
            "upperLimitPrice": "93000",
            "lowerLimitPrice": "50400",
            "currency": "KRW",
        }

    def stock_warnings(self, symbol: str) -> list[dict]:
        self.calls.append(("stock_warnings", symbol))
        return [{"warningType": "VI_DYNAMIC", "startDate": "2026-08-14", "endDate": None}]

    def investor_trading(self, symbol: str, *, count: int = 1) -> dict:
        self.calls.append(("investor_trading", symbol))
        del count
        return {
            "records": [
                {
                    "date": "2026-08-14",
                    "foreigner": {"netBuyVolume": "119900"},
                    "institution": {"netBuyVolume": "-50000"},
                    "individual": None,
                }
            ]
        }

    def program_trades(self, symbol: str, *, count: int = 1) -> dict:
        self.calls.append(("program_trades", symbol))
        del count
        return {"records": [{"date": "2026-08-14", "arbitrage": {"netBuyVolume": "10"}}]}

    def short_selling(self, symbol: str, *, count: int = 1) -> dict:
        self.calls.append(("short_selling", symbol))
        del count
        return {"records": [{"date": "2026-08-14", "shortSellingVolumeRate": "0.02"}]}

    def credit_trades(self, symbol: str, *, count: int = 1) -> dict:
        self.calls.append(("credit_trades", symbol))
        del count
        return {"records": [{"date": "2026-08-14", "marginLoan": {"balanceQuantity": "1"}}]}

    def securities_lending(self, symbol: str, *, count: int = 1) -> dict:
        self.calls.append(("securities_lending", symbol))
        del count
        return {"records": [{"date": "2026-08-14", "balanceQuantity": "2"}]}


class BrokenPricesClient(FakeContextClient):
    def prices(self, symbols: tuple[str, ...]) -> list[dict]:
        self.calls.append(("prices", ",".join(symbols)))
        raise TossApiError(status=500, code="internal-error", message="down")


class MarketContextCollectorTest(unittest.TestCase):
    def test_compacts_kr_market_and_stock_info_for_a_signal(self) -> None:
        client = FakeContextClient()
        snapshot = MarketContextCollector(client).collect("005930")

        self.assertEqual(snapshot.symbol, "005930")
        self.assertEqual(snapshot.errors, ())
        self.assertEqual(snapshot.payload["price"]["lastPrice"], "72000")
        self.assertEqual(snapshot.payload["orderbook"]["bestAsk"], "72100")
        self.assertEqual(snapshot.payload["orderbook"]["bestBid"], "72000")
        self.assertEqual(snapshot.payload["trades"]["lastPrice"], "72000")
        self.assertEqual(snapshot.payload["priceLimits"]["upperLimitPrice"], "93000")
        self.assertEqual(snapshot.payload["warnings"], ["VI_DYNAMIC"])
        self.assertEqual(snapshot.payload["investorTrading"]["foreignerNet"], "119900")
        self.assertIn("investor_trading", [name for name, _ in client.calls])

    def test_skips_kr_only_trading_trends_for_us_symbols(self) -> None:
        client = FakeContextClient()
        snapshot = MarketContextCollector(client).collect("AAPL")

        trend_calls = [
            name
            for name, _ in client.calls
            if name
            in {
                "investor_trading",
                "program_trades",
                "short_selling",
                "credit_trades",
                "securities_lending",
            }
        ]
        self.assertEqual(trend_calls, [])
        self.assertNotIn("investorTrading", snapshot.payload)

    def test_keeps_partial_context_when_one_endpoint_fails(self) -> None:
        client = BrokenPricesClient()
        snapshot = MarketContextCollector(client).collect("005930")

        self.assertTrue(snapshot.errors)
        self.assertEqual(snapshot.payload["warnings"], ["VI_DYNAMIC"])
        self.assertNotIn("price", snapshot.payload)


if __name__ == "__main__":
    unittest.main()
