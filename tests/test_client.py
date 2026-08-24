import json
import unittest
from collections import deque
from datetime import date

from toss_trader.client import HttpRequest, HttpResponse, TossClient
from toss_trader.errors import TossApiError


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest, timeout: float) -> HttpResponse:
        self.requests.append(request)
        return self.responses.popleft()


def response(status: int, payload: dict, **headers: str) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers,
        body=json.dumps(payload).encode(),
    )


class TossClientTest(unittest.TestCase):
    def test_fetches_market_rankings_with_safety_filter(self) -> None:
        transport = FakeTransport(
            [
                response(
                    200,
                    {"access_token": "token", "token_type": "Bearer", "expires_in": 3600},
                ),
                response(
                    200,
                    {"result": {"rankedAt": None, "rankings": []}},
                ),
            ]
        )
        client = TossClient(client_id="id", client_secret="secret", transport=transport)

        result = client.rankings(
            ranking_type="MARKET_TRADING_AMOUNT",
            market_country="KR",
            duration="realtime",
            exclude_investment_caution=True,
            count=30,
        )

        self.assertEqual(result["rankings"], [])
        url = transport.requests[-1].url
        self.assertIn("/api/v1/rankings?", url)
        self.assertIn("excludeInvestmentCaution=true", url)

    def test_spaces_candle_requests_and_honors_rate_limit_reset(self) -> None:
        now = [100.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        transport = FakeTransport(
            [
                response(
                    200,
                    {"access_token": "token", "token_type": "Bearer", "expires_in": 3600},
                ),
                response(
                    200,
                    {"result": {"candles": [], "nextBefore": None}},
                    **{
                        "X-RateLimit-Limit": "10",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1",
                    },
                ),
                response(
                    200,
                    {"result": {"candles": [], "nextBefore": None}},
                ),
            ]
        )
        client = TossClient(
            client_id="id",
            client_secret="secret",
            transport=transport,
            clock=lambda: now[0],
            sleeper=sleep,
            candle_min_interval_seconds=0.25,
        )

        client.candles("005930", count=1)
        client.candles("000660", count=1)

        self.assertEqual(sleeps, [1.0])

    def test_fetches_stock_names_in_one_batch(self) -> None:
        transport = FakeTransport(
            [
                response(
                    200,
                    {"access_token": "token", "token_type": "Bearer", "expires_in": 3600},
                ),
                response(
                    200,
                    {"result": [{"symbol": "005930", "name": "삼성전자"}]},
                ),
            ]
        )
        client = TossClient(client_id="id", client_secret="secret", transport=transport)

        result = client.stocks(("005930",))

        self.assertEqual(result[0]["name"], "삼성전자")
        self.assertTrue(transport.requests[-1].url.endswith("/api/v1/stocks?symbols=005930"))

    def test_fetches_market_calendar_for_country_and_date(self) -> None:
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "access_token": "token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                ),
                response(200, {"result": {"today": {"date": "2026-08-12"}}}),
            ]
        )
        client = TossClient(
            client_id="id",
            client_secret="secret",
            transport=transport,
        )

        result = client.market_calendar("kr", day=date(2026, 8, 12))

        self.assertEqual(result["today"]["date"], "2026-08-12")
        self.assertTrue(
            transport.requests[-1].url.endswith(
                "/api/v1/market-calendar/KR?date=2026-08-12"
            )
        )

    def test_rejects_unsupported_market_calendar_country(self) -> None:
        client = TossClient(
            client_id="id",
            client_secret="secret",
            transport=FakeTransport([]),
        )

        with self.assertRaises(ValueError):
            client.market_calendar("JP")

    def test_gets_token_once_and_reuses_it_for_prices(self) -> None:
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "access_token": "secret-token",
                        "token_type": "Bearer",
                        "expires_in": 86400,
                    },
                ),
                response(
                    200,
                    {
                        "result": [
                            {
                                "symbol": "005930",
                                "timestamp": "2026-08-12T09:30:00+09:00",
                                "lastPrice": "71000",
                                "currency": "KRW",
                            }
                        ]
                    },
                ),
                response(200, {"result": []}),
            ]
        )
        client = TossClient(
            client_id="client-id",
            client_secret="client-secret",
            transport=transport,
            clock=lambda: 100.0,
        )

        prices = client.prices(["005930"])
        client.accounts()

        self.assertEqual(prices[0]["lastPrice"], "71000")
        self.assertEqual(len(transport.requests), 3)
        token_request = transport.requests[0]
        self.assertEqual(token_request.method, "POST")
        self.assertTrue(token_request.url.endswith("/oauth2/token"))
        self.assertIn(b"grant_type=client_credentials", token_request.body or b"")
        self.assertIn(b"client_id=client-id", token_request.body or b"")
        self.assertEqual(
            transport.requests[1].headers["Authorization"],
            "Bearer secret-token",
        )
        self.assertNotIn("X-Tossinvest-Account", transport.requests[2].headers)

    def test_account_header_is_added_only_to_account_scoped_calls(self) -> None:
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "access_token": "token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                ),
                response(200, {"result": {"items": []}}),
            ]
        )
        client = TossClient(
            client_id="id",
            client_secret="secret",
            account_seq="7",
            transport=transport,
        )

        client.holdings()

        self.assertEqual(transport.requests[-1].headers["X-Tossinvest-Account"], "7")

    def test_retries_get_after_retry_after_on_rate_limit(self) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "access_token": "token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                ),
                response(
                    429,
                    {"error": {"code": "rate-limit-exceeded", "message": "slow"}},
                    **{"Retry-After": "0.25"},
                ),
                response(200, {"result": []}),
            ]
        )
        client = TossClient(
            client_id="id",
            client_secret="secret",
            transport=transport,
            sleeper=sleeps.append,
            max_get_retries=1,
        )

        self.assertEqual(client.prices(["AAPL"]), [])
        self.assertEqual(sleeps, [0.25])

    def test_raises_structured_api_error(self) -> None:
        transport = FakeTransport(
            [
                response(
                    200,
                    {
                        "access_token": "token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                ),
                response(
                    404,
                    {
                        "error": {
                            "requestId": "request-1",
                            "code": "stock-not-found",
                            "message": "missing",
                        }
                    },
                ),
            ]
        )
        client = TossClient(
            client_id="id",
            client_secret="secret",
            transport=transport,
        )

        with self.assertRaises(TossApiError) as caught:
            client.prices(["NOPE"])

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "stock-not-found")
        self.assertEqual(caught.exception.request_id, "request-1")

    def test_rejects_invalid_or_oversized_symbol_batch_without_network(self) -> None:
        client = TossClient(
            client_id="id",
            client_secret="secret",
            transport=FakeTransport([]),
        )

        with self.assertRaises(ValueError):
            client.prices(["005930;DROP"])
        with self.assertRaises(ValueError):
            client.prices(["AAPL"] * 201)

    def test_urllib_transport_retries_transient_dns_error_and_succeeds(self) -> None:
        from urllib.error import URLError
        from toss_trader.client import HttpRequest, UrllibTransport

        sleeps: list[float] = []
        call_count = [0]

        class FakeHttpResponse:
            status = 200
            headers = {}

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(_req, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                raise URLError("[Errno -2] Name or service not known")
            return FakeHttpResponse()

        transport = UrllibTransport(
            max_retries=1,
            retry_delay=0.5,
            sleeper=sleeps.append,
            urlopen_fn=fake_urlopen,
        )
        res = transport.send(HttpRequest(method="GET", url="https://openapi.tossinvest.com", headers={}), timeout=5.0)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.body, b'{"ok": true}')
        self.assertEqual(call_count[0], 2)
        self.assertEqual(sleeps, [0.5])

    def test_urllib_transport_raises_when_all_retries_exhausted(self) -> None:
        from urllib.error import URLError
        from toss_trader.client import HttpRequest, UrllibTransport

        sleeps: list[float] = []

        def fake_urlopen_fail(_req, timeout):
            raise URLError("[Errno -2] Name or service not known")

        transport = UrllibTransport(
            max_retries=1,
            retry_delay=0.5,
            sleeper=sleeps.append,
            urlopen_fn=fake_urlopen_fail,
        )
        with self.assertRaises(URLError):
            transport.send(HttpRequest(method="GET", url="https://openapi.tossinvest.com", headers={}), timeout=5.0)
        self.assertEqual(sleeps, [0.5])


if __name__ == "__main__":
    unittest.main()
