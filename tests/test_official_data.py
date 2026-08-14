from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from toss_trader.official_data import (
    FinancialFact,
    OfficialDataRepository,
    compute_ttm_eps,
    next_session_available_at,
)


class OfficialDataTest(unittest.TestCase):
    def test_next_session_skips_weekend_and_holiday_from_observed_sessions(self) -> None:
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
            connection.close()
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
                [{
                    "symbol": "005930", "corp_code": "00126380",
                    "receipt_no": "20260727000002", "receipt_date": "2026-07-27",
                    "report_name": "임원ㆍ주요주주특정증권등소유상황보고서",
                    "available_at": "2026-07-28T08:00:00+09:00",
                    "blocked_through": "2026-07-30T08:00:00+09:00",
                    "is_entry_blocking": 0, "is_preannounced": 0,
                    "scheduled_for": None, "source": "opendart:list",
                    "retrieved_at": "2026-08-15T00:00:00+00:00",
                    "payload_hash": "ghi",
                }]
            )
            self.assertFalse(
                repository.event_blocks_entry("005930", "2026-07-28T09:00:00+09:00")
            )
            repository.close()

    def test_ttm_snapshot_does_not_backfill_later_correction(self) -> None:
        def eps(year: int, code: str, amount: int, receipt: str, available: str) -> FinancialFact:
            return FinancialFact(
                symbol="005930", corp_code="00126380", business_year=year,
                report_code=code, fs_div="CFS", statement_division="IS",
                account_id="ifrs-full_BasicEarningsLossPerShare",
                account_name="기본주당이익", amount=Decimal(amount), currency="KRW",
                receipt_no=receipt, receipt_date=date.fromisoformat(receipt[:4] + "-" + receipt[4:6] + "-" + receipt[6:8]),
                available_at=available, source="opendart:fnlttSinglAcntAll",
                retrieved_at="2026-08-15T00:00:00+00:00", payload_hash=receipt,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            repository = OfficialDataRepository(str(path))
            repository.upsert_financial_facts(
                [
                    eps(2023, "11011", 100, "20240301000001", "2024-03-04T08:00:00+09:00"),
                    eps(2023, "11011", 200, "20250301000002", "2025-03-04T08:00:00+09:00"),
                    eps(2023, "11012", 40, "20230801000003", "2023-08-02T08:00:00+09:00"),
                    eps(2024, "11012", 60, "20240801000004", "2024-08-02T08:00:00+09:00"),
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
