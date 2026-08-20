import json
import unittest
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

from toss_trader.krx_openapi import (
    fetch_krx_acc_trdval_rankings,
    krx_rows_to_rankings,
)


class KrxOpenApiTest(unittest.TestCase):
    def test_merges_markets_sorts_amount_and_skips_non_six_codes(self) -> None:
        payload = krx_rows_to_rankings(
            [
                {
                    "ISU_CD": "005930",
                    "TDD_CLSPRC": "71000",
                    "FLUC_RT": "2.00",
                    "ACC_TRDVAL": "1000000000",
                },
                {
                    "ISU_CD": "0004V0",
                    "TDD_CLSPRC": "1",
                    "FLUC_RT": "0",
                    "ACC_TRDVAL": "9999999999",
                },
                {
                    "ISU_CD": "12345",
                    "TDD_CLSPRC": "1",
                    "FLUC_RT": "0",
                    "ACC_TRDVAL": "9999999998",
                },
            ],
            [
                {
                    "ISU_CD": "000660",
                    "TDD_CLSPRC": "190000",
                    "FLUC_RT": "-1.50",
                    "ACC_TRDVAL": "2,000,000,000",
                }
            ],
            count=2,
            ranked_at=datetime(2026, 8, 20, 3, 0, tzinfo=UTC),
        )

        self.assertEqual(
            [item["symbol"] for item in payload["rankings"]],
            ["000660", "005930"],
        )
        self.assertEqual(payload["rankings"][0]["rank"], 1)
        self.assertEqual(payload["rankings"][0]["tradingAmount"], "2000000000")
        self.assertEqual(payload["rankings"][0]["price"]["changeRate"], "-0.015")
        self.assertEqual(payload["rankings"][1]["price"]["changeRate"], "0.02")
        self.assertEqual(payload["rankedAt"], "2026-08-20T03:00:00+00:00")

    def test_empty_blocks_are_data_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "krx daily rankings are empty"):
            krx_rows_to_rankings([], [], count=10, ranked_at=datetime.now(UTC))

    def test_fetch_requests_prior_session_and_maps_auth_header(self) -> None:
        calls: list[Request] = []

        def fake_urlopen(request: Request, timeout: float = 0) -> BytesIO:
            calls.append(request)
            path = request.full_url
            if "stk_bydd_trd" in path:
                body = {
                    "OutBlock_1": [
                        {
                            "ISU_CD": "005930",
                            "TDD_CLSPRC": "1",
                            "FLUC_RT": "0",
                            "ACC_TRDVAL": "10",
                        }
                    ]
                }
            else:
                body = {"OutBlock_1": []}
            return BytesIO(json.dumps(body).encode())

        payload = fetch_krx_acc_trdval_rankings(
            api_key="secret-key",
            bas_dd="20260819",
            count=1,
            ranked_at=datetime(2026, 8, 20, 3, 0, tzinfo=UTC),
            urlopen=fake_urlopen,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(item.get_header("Auth_key") == "secret-key" for item in calls)
        )
        self.assertTrue(all("basDd=20260819" in item.full_url for item in calls))
        self.assertNotIn("secret-key", calls[0].full_url)
        self.assertEqual(payload["rankings"][0]["symbol"], "005930")

    def test_unauthorized_is_fail_closed(self) -> None:
        def fake_urlopen(request: Request, timeout: float = 0) -> BytesIO:
            raise HTTPError(
                request.full_url, 401, "Unauthorized", hdrs={}, fp=BytesIO()
            )

        with self.assertRaisesRegex(RuntimeError, "unauthorized"):
            fetch_krx_acc_trdval_rankings(
                api_key="secret-key",
                bas_dd="20260819",
                count=1,
                ranked_at=datetime.now(UTC),
                urlopen=fake_urlopen,
            )


if __name__ == "__main__":
    unittest.main()
