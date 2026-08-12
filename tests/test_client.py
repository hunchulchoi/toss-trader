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


if __name__ == "__main__":
    unittest.main()
