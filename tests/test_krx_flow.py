from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from toss_trader.krx_flow import KrxInvestorFlowCsvImporter
from toss_trader.official_data import OfficialDataRepository


class KrxFlowImportTest(unittest.TestCase):
    def test_imports_matching_official_csv_with_observed_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = OfficialDataRepository(str(root / "market.db"))
            repository.upsert_universe_rows(
                [
                    {
                        "session_date": "2026-08-18",
                        "symbol": symbol,
                        "isin_code": f"KR7{symbol}003",
                        "display_name": symbol,
                        "market_category": "KOSPI",
                        "close_price": "100",
                        "market_cap": "1",
                        "trading_value": trading_value,
                        "listed_share_count": "1",
                        "security_type": "COMMON",
                        "source": "datago",
                        "source_record_id": symbol,
                        "published_at": None,
                        "available_at": None,
                        "retrieved_at": "2026-08-18T09:00:00+00:00",
                        "payload_hash": symbol,
                    }
                    for symbol, trading_value in (("005930", "1000"), ("000660", "2000"))
                ]
            )
            foreign = root / "foreign.csv"
            institutional = root / "institutional.csv"
            foreign.write_text(
                "종목코드,종목명,순매수거래대금\n005930,삼성전자,100\n000660,SK하이닉스,-20\n",
                encoding="utf-8-sig",
            )
            institutional.write_text(
                "종목코드,종목명,순매수거래대금\n005930,삼성전자,-50\n000660,SK하이닉스,30\n",
                encoding="utf-8-sig",
            )
            result = KrxInvestorFlowCsvImporter(repository).import_files(
                session_date=date(2026, 8, 18),
                foreign_csv=foreign,
                institutional_csv=institutional,
                target_symbols=["005930", "000660", "AAPL"],
                retrieved_at=datetime(2026, 8, 19, 0, tzinfo=UTC),
            )
            second = KrxInvestorFlowCsvImporter(repository).import_files(
                session_date=date(2026, 8, 18),
                foreign_csv=foreign,
                institutional_csv=institutional,
                target_symbols=["005930", "000660"],
                retrieved_at=datetime(2026, 8, 19, 1, tzinfo=UTC),
            )
            repository.close()

            connection = sqlite3.connect(root / "market.db")
            rows = connection.execute(
                """SELECT symbol, available_at, foreign_net_buy,
                          institutional_net_buy, trading_value, source
                FROM market_flow_pit_v2 ORDER BY symbol"""
            ).fetchall()
            coverage = connection.execute(
                """SELECT completed_at, row_count, source
                FROM market_pit_coverage WHERE dataset='flow_krx'"""
            ).fetchone()
            connection.close()

        self.assertEqual(result.inserted_rows, 2)
        self.assertEqual(result.target_symbols, 2)
        self.assertEqual(second.inserted_rows, 0)
        self.assertEqual(
            coverage,
            ("2026-08-19T00:00:00+00:00", 2, "krx:manual-csv"),
        )
        self.assertEqual(
            rows,
            [
                ("000660", "2026-08-19T00:00:00+00:00", "-20", "30", "2000", "krx:manual-csv"),
                ("005930", "2026-08-19T00:00:00+00:00", "100", "-50", "1000", "krx:manual-csv"),
            ],
        )

    def test_rejects_missing_target_symbol_without_partial_insert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = OfficialDataRepository(str(root / "market.db"))
            foreign = root / "foreign.csv"
            institutional = root / "institutional.csv"
            foreign.write_text("종목코드,순매수거래대금\n005930,100\n", encoding="utf-8")
            institutional.write_text(
                "종목코드,순매수거래대금\n000660,100\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "coverage mismatch"):
                KrxInvestorFlowCsvImporter(repository).import_files(
                    session_date=date(2026, 8, 18),
                    foreign_csv=foreign,
                    institutional_csv=institutional,
                    target_symbols=["005930", "000660"],
                )
            repository.close()
