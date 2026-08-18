from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from toss_trader.client import HttpResponse
from toss_trader.kis_flow import (
    KisInvestorFlowClient,
    KisInvestorFlowCollector,
    KisTokenRequestError,
)
from toss_trader.official_data import (
    FinancialFact,
    OfficialDataCollector,
    OfficialDataRepository,
    compute_ttm_eps,
    next_session_available_at,
)


class OfficialDataTest(unittest.TestCase):
    def test_kis_flow_uses_official_tr_and_parses_daily_amounts(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.requests = []

            def send(self, request, timeout):
                del timeout
                self.requests.append(request)
                if request.url.endswith("/oauth2/tokenP"):
                    return HttpResponse(
                        200,
                        {},
                        b'{"access_token":"memory-only","expires_in":86400}',
                    )
                return HttpResponse(
                    200,
                    {},
                    (
                        b'{"rt_cd":"0","output2":[{'
                        b'"stck_bsop_date":"20260817",'
                        b'"frgn_ntby_tr_pbmn":"-1200",'
                        b'"orgn_ntby_tr_pbmn":"3400",'
                        b'"acml_tr_pbmn":"987654"}]}'
                    ),
                )

        transport = FakeTransport()
        client = KisInvestorFlowClient(
            app_key="app-key", app_secret="app-secret", transport=transport
        )

        rows = client.daily_investor_flow("005930", as_of=date(2026, 8, 18))

        self.assertEqual(rows[0]["stck_bsop_date"], "20260817")
        request = transport.requests[1]
        self.assertIn("FID_INPUT_ISCD=005930", request.url)
        self.assertEqual(request.headers["tr_id"], "FHPTJ04160001")
        self.assertEqual(request.headers["authorization"], "Bearer memory-only")

    def test_kis_token_cooldown_retries_once(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.requests = []

            def send(self, request, timeout):
                del timeout
                self.requests.append(request)
                if len(self.requests) == 1:
                    return HttpResponse(429, {}, b'{"error_code":"EGW00133"}')
                return HttpResponse(
                    200,
                    {},
                    b'{"access_token":"memory-only","expires_in":86400}',
                )

        waits: list[float] = []
        client = KisInvestorFlowClient(
            app_key="app-key",
            app_secret="app-secret",
            transport=FakeTransport(),
            sleeper=waits.append,
        )

        self.assertEqual(client._get_access_token(), "memory-only")
        self.assertEqual(waits, [61.0])

    def test_kis_collector_keeps_first_observed_availability(self) -> None:
        class FakeClient:
            def daily_investor_flow(self, symbol, *, as_of):
                del symbol, as_of
                return [
                    {
                        "stck_bsop_date": "20260817",
                        "frgn_ntby_tr_pbmn": "-1,200",
                        "orgn_ntby_tr_pbmn": "3,400",
                        "acml_tr_pbmn": "987654",
                    },
                    {
                        "stck_bsop_date": "20260818",
                        "frgn_ntby_tr_pbmn": "10",
                        "orgn_ntby_tr_pbmn": "20",
                        "acml_tr_pbmn": "30",
                    },
                ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            repository = OfficialDataRepository(str(path))
            repository.upsert_universe_rows(
                [
                    {
                        "session_date": session,
                        "symbol": "005930",
                        "isin_code": "KR7005930003",
                        "display_name": "삼성전자",
                        "market_category": "KOSPI",
                        "close_price": "100",
                        "market_cap": "1",
                        "trading_value": "987654",
                        "listed_share_count": "1",
                        "security_type": "COMMON",
                        "source": "test",
                        "source_record_id": session,
                        "published_at": None,
                        "available_at": None,
                        "retrieved_at": "2026-08-18T00:00:00+00:00",
                        "payload_hash": session,
                    }
                    for session in ("2026-08-14", "2026-08-17")
                ]
            )
            collector = KisInvestorFlowCollector(FakeClient(), repository)
            first = collector.collect(
                symbols=["005930"],
                as_of=date(2026, 8, 18),
                completed_through=date(2026, 8, 17),
                retrieved_at=datetime(2026, 8, 18, 9, tzinfo=UTC),
            )
            second = collector.collect(
                symbols=["005930"],
                as_of=date(2026, 8, 18),
                completed_through=date(2026, 8, 17),
                retrieved_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
            )
            repository.close()

            connection = sqlite3.connect(path)
            row = connection.execute(
                "SELECT session_index, available_at, foreign_net_buy, "
                "institutional_net_buy, trading_value, source "
                "FROM market_flow_pit_v2"
            ).fetchone()
            connection.close()
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            self.assertEqual(
                row,
                (
                    2,
                    "2026-08-18T09:00:00+00:00",
                    "-1200",
                    "3400",
                    "987654",
                    "kis:FHPTJ04160001",
                ),
            )

    def test_kis_collector_skips_invalid_and_failed_symbols(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.symbols: list[str] = []

            def daily_investor_flow(self, symbol, *, as_of):
                del as_of
                self.symbols.append(symbol)
                if symbol == "000001":
                    raise RuntimeError("KIS API error temporary")
                return [
                    {
                        "stck_bsop_date": "20260817",
                        "frgn_ntby_tr_pbmn": "1",
                        "orgn_ntby_tr_pbmn": "2",
                        "acml_tr_pbmn": "3",
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            repository = OfficialDataRepository(str(Path(directory) / "market.db"))
            repository.upsert_universe_rows(
                [
                    {
                        "session_date": "2026-08-17",
                        "symbol": "005930",
                        "isin_code": "KR7005930003",
                        "display_name": "삼성전자",
                        "market_category": "KOSPI",
                        "close_price": "100",
                        "market_cap": "1",
                        "trading_value": "3",
                        "listed_share_count": "1",
                        "security_type": "COMMON",
                        "source": "test",
                        "source_record_id": "2026-08-17",
                        "published_at": None,
                        "available_at": None,
                        "retrieved_at": "2026-08-18T00:00:00+00:00",
                        "payload_hash": "test",
                    }
                ]
            )
            client = FakeClient()
            stored = KisInvestorFlowCollector(client, repository).collect(
                symbols=["000001", "AAPL", "005930"],
                as_of=date(2026, 8, 18),
                completed_through=date(2026, 8, 17),
                retrieved_at=datetime(2026, 8, 18, 9, tzinfo=UTC),
            )
            repository.close()

        self.assertEqual(client.symbols, ["000001", "005930"])
        self.assertEqual(stored, 1)

    def test_kis_collector_stops_after_token_failure(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.symbols: list[str] = []

            def daily_investor_flow(self, symbol, *, as_of):
                del as_of
                self.symbols.append(symbol)
                raise KisTokenRequestError("KIS token request failed: EGW00133")

        with tempfile.TemporaryDirectory() as directory:
            repository = OfficialDataRepository(str(Path(directory) / "market.db"))
            client = FakeClient()
            stored = KisInvestorFlowCollector(client, repository).collect(
                symbols=["005930", "000660"],
                as_of=date(2026, 8, 18),
                completed_through=date(2026, 8, 17),
                retrieved_at=datetime(2026, 8, 18, 9, tzinfo=UTC),
            )
            repository.close()

        self.assertEqual(client.symbols, ["005930"])
        self.assertEqual(stored, 0)

    def test_event_collection_checkpoints_each_date_and_resumes(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[date] = []

            def dart_events(self, *, start, end, page=1, page_count=100):
                del end, page, page_count
                self.calls.append(start)
                return {
                    "status": "000",
                    "total_page": 1,
                    "list": [
                        {
                            "stock_code": "005930",
                            "corp_code": "00126380",
                            "rcept_no": start.strftime("%Y%m%d") + "000001",
                            "rcept_dt": start.strftime("%Y%m%d"),
                            "report_nm": "유상증자결정",
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            repository = OfficialDataRepository(str(Path(directory) / "market.db"))
            client = FakeClient()
            collector = OfficialDataCollector(client, repository)
            sessions = [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]

            first = collector.collect_events(
                start=date(2026, 8, 17),
                end=date(2026, 8, 18),
                additional_sessions=sessions,
            )
            second = collector.collect_events(
                start=date(2026, 8, 17),
                end=date(2026, 8, 18),
                additional_sessions=sessions,
            )

            self.assertEqual(first, 2)
            self.assertEqual(second, 0)
            self.assertEqual(client.calls, [date(2026, 8, 17), date(2026, 8, 18)])
            self.assertEqual(
                repository.covered_dates(
                    dataset="events",
                    start=date(2026, 8, 17),
                    end=date(2026, 8, 18),
                ),
                {date(2026, 8, 17), date(2026, 8, 18)},
            )
            repository.close()

    def test_next_session_skips_weekend_and_holiday_from_observed_sessions(
        self,
    ) -> None:
        sessions = [date(2025, 8, 14), date(2025, 8, 18)]

        available = next_session_available_at(date(2025, 8, 14), sessions)

        self.assertEqual(available.isoformat(), "2025-08-18T08:00:00+09:00")
        self.assertIsNone(next_session_available_at(date(2025, 8, 18), sessions))

    def test_ttm_eps_uses_annual_plus_current_minus_prior_comparable(self) -> None:
        self.assertEqual(
            compute_ttm_eps(
                report_code="11012",
                current_cumulative=Decimal(700),
                prior_annual=Decimal(1000),
                prior_comparable=Decimal(400),
            ),
            Decimal(1300),
        )
        self.assertEqual(
            compute_ttm_eps(
                report_code="11011",
                current_cumulative=Decimal(1200),
                prior_annual=None,
                prior_comparable=None,
            ),
            Decimal(1200),
        )
        self.assertIsNone(
            compute_ttm_eps(
                report_code="11014",
                current_cumulative=Decimal(900),
                prior_annual=None,
                prior_comparable=Decimal(600),
            )
        )

    def test_repository_preserves_cfs_and_revision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            repository = OfficialDataRepository(str(path))
            fact = FinancialFact(
                symbol="005930",
                corp_code="00126380",
                business_year=2025,
                report_code="11011",
                fs_div="CFS",
                statement_division="IS",
                account_id="ifrs-full_BasicEarningsLossPerShare",
                account_name="기본주당이익(손실)",
                amount=Decimal(6605),
                currency="KRW",
                receipt_no="20260310002820",
                receipt_date=date(2026, 3, 10),
                available_at="2026-03-11T08:00:00+09:00",
                source="opendart:f nlttSinglAcntAll".replace(" ", ""),
                retrieved_at="2026-08-15T00:00:00+00:00",
                payload_hash="abc",
            )

            self.assertEqual(repository.upsert_financial_facts([fact]), 1)
            self.assertEqual(repository.rebuild_valuation_snapshots(), 1)
            repository.close()

            connection = sqlite3.connect(path)
            row = connection.execute(
                "SELECT fs_div, rcept_no, account_id, available_at "
                "FROM market_financial_facts_v2"
            ).fetchone()
            snapshot = connection.execute(
                "SELECT ttm_eps, trailing_eps_growth_yoy, status, method "
                "FROM market_valuation_snapshots_v2"
            ).fetchone()
            self.assertEqual(
                row,
                (
                    "CFS",
                    "20260310002820",
                    "ifrs-full_BasicEarningsLossPerShare",
                    "2026-03-11T08:00:00+09:00",
                ),
            )
            self.assertEqual(
                snapshot,
                ("6605", None, "VALID_EPS_ONLY", "DART_CUMULATIVE_EPS_TTM_V1"),
            )
            flow_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE name='market_flow_pit_v2'"
            ).fetchone()
            coverage_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE name='market_pit_coverage'"
            ).fetchone()
            connection.close()
            self.assertIsNotNone(flow_table)
            self.assertIsNotNone(coverage_table)

    def test_unscheduled_event_cannot_be_blocked_before_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            repository = OfficialDataRepository(str(path))
            repository.upsert_events(
                [
                    {
                        "symbol": "000150",
                        "corp_code": "00117212",
                        "receipt_no": "20260727000001",
                        "receipt_date": "2026-07-27",
                        "report_name": "유상증자결정",
                        "available_at": "2026-07-28T08:00:00+09:00",
                        "blocked_through": "2026-07-30T08:00:00+09:00",
                        "is_entry_blocking": 1,
                        "is_preannounced": 0,
                        "scheduled_for": None,
                        "source": "opendart:list",
                        "retrieved_at": "2026-08-15T00:00:00+00:00",
                        "payload_hash": "def",
                    }
                ]
            )

            self.assertFalse(
                repository.event_blocks_entry("000150", "2026-07-27T09:00:00+09:00")
            )
            self.assertTrue(
                repository.event_blocks_entry("000150", "2026-07-28T09:00:00+09:00")
            )
            self.assertFalse(
                repository.event_blocks_entry("000150", "2026-07-30T09:00:00+09:00")
            )
            repository.upsert_events(
                [
                    {
                        "symbol": "005930",
                        "corp_code": "00126380",
                        "receipt_no": "20260727000002",
                        "receipt_date": "2026-07-27",
                        "report_name": "임원ㆍ주요주주특정증권등소유상황보고서",
                        "available_at": "2026-07-28T08:00:00+09:00",
                        "blocked_through": "2026-07-30T08:00:00+09:00",
                        "is_entry_blocking": 0,
                        "is_preannounced": 0,
                        "scheduled_for": None,
                        "source": "opendart:list",
                        "retrieved_at": "2026-08-15T00:00:00+00:00",
                        "payload_hash": "ghi",
                    }
                ]
            )
            self.assertFalse(
                repository.event_blocks_entry("005930", "2026-07-28T09:00:00+09:00")
            )
            repository.close()

    def test_ttm_snapshot_does_not_backfill_later_correction(self) -> None:
        def eps(
            year: int, code: str, amount: int, receipt: str, available: str
        ) -> FinancialFact:
            return FinancialFact(
                symbol="005930",
                corp_code="00126380",
                business_year=year,
                report_code=code,
                fs_div="CFS",
                statement_division="IS",
                account_id="ifrs-full_BasicEarningsLossPerShare",
                account_name="기본주당이익",
                amount=Decimal(amount),
                currency="KRW",
                receipt_no=receipt,
                receipt_date=date.fromisoformat(
                    receipt[:4] + "-" + receipt[4:6] + "-" + receipt[6:8]
                ),
                available_at=available,
                source="opendart:fnlttSinglAcntAll",
                retrieved_at="2026-08-15T00:00:00+00:00",
                payload_hash=receipt,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            repository = OfficialDataRepository(str(path))
            repository.upsert_financial_facts(
                [
                    eps(
                        2023,
                        "11011",
                        100,
                        "20240301000001",
                        "2024-03-04T08:00:00+09:00",
                    ),
                    eps(
                        2023,
                        "11011",
                        200,
                        "20250301000002",
                        "2025-03-04T08:00:00+09:00",
                    ),
                    eps(
                        2023, "11012", 40, "20230801000003", "2023-08-02T08:00:00+09:00"
                    ),
                    eps(
                        2024, "11012", 60, "20240801000004", "2024-08-02T08:00:00+09:00"
                    ),
                ]
            )
            repository.rebuild_valuation_snapshots()
            repository.close()
            connection = sqlite3.connect(path)
            row = connection.execute(
                "SELECT ttm_eps FROM market_valuation_snapshots_v2 "
                "WHERE business_year=2024 AND report_code='11012'"
            ).fetchone()
            connection.close()
            self.assertEqual(row, ("120",))


if __name__ == "__main__":
    unittest.main()
