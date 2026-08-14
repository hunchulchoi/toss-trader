from __future__ import annotations

import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import urlencode
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from .client import HttpRequest, Transport, UrllibTransport

SEOUL = ZoneInfo("Asia/Seoul")
OPENDART_BASE_URL = "https://opendart.fss.or.kr/api"
DATAGO_PRICE_URL = (
    "https://apis.data.go.kr/1160100/service/"
    "GetStockSecuritiesInfoService/getStockPriceInfo"
)

FINANCIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_financial_facts_v2 (
    symbol TEXT NOT NULL,
    corp_code TEXT NOT NULL,
    business_year INTEGER NOT NULL,
    report_code TEXT NOT NULL,
    fs_div TEXT NOT NULL CHECK (fs_div IN ('CFS', 'OFS')),
    statement_division TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    amount TEXT,
    currency TEXT,
    rcept_no TEXT NOT NULL,
    rcept_dt TEXT NOT NULL,
    available_at TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (symbol, business_year, report_code, fs_div, account_id, rcept_no)
)
"""

EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_events_pit_v2 (
    symbol TEXT NOT NULL,
    corp_code TEXT NOT NULL,
    rcept_no TEXT PRIMARY KEY,
    rcept_dt TEXT NOT NULL,
    report_name TEXT NOT NULL,
    available_at TEXT,
    blocked_through TEXT,
    is_entry_blocking INTEGER NOT NULL CHECK (is_entry_blocking IN (0, 1)),
    is_preannounced INTEGER NOT NULL CHECK (is_preannounced IN (0, 1)),
    scheduled_for TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    CHECK (is_preannounced = 1 OR scheduled_for IS NULL)
)
"""

VALUATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_valuation_snapshots_v2 (
    symbol TEXT NOT NULL,
    business_year INTEGER NOT NULL,
    report_code TEXT NOT NULL,
    fs_div TEXT NOT NULL,
    rcept_no TEXT NOT NULL,
    available_at TEXT NOT NULL,
    ttm_eps TEXT,
    trailing_eps_growth_yoy TEXT,
    owner_equity TEXT,
    listed_share_count TEXT,
    bps TEXT,
    reference_price TEXT,
    trailing_per TEXT,
    pbr TEXT,
    bps_method TEXT,
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    PRIMARY KEY (symbol, business_year, report_code, fs_div, rcept_no)
)
"""

UNIVERSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_universe_raw_v2 (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    isin_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    market_category TEXT NOT NULL,
    close_price TEXT,
    market_cap TEXT,
    trading_value TEXT,
    listed_share_count TEXT,
    security_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    published_at TEXT,
    available_at TEXT,
    retrieved_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (session_date, symbol, source)
)
"""

OFFICIAL_INDEXES = (
    (
        "CREATE INDEX IF NOT EXISTS market_universe_v2_symbol_available_idx "
        "ON market_universe_raw_v2(symbol, available_at DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS market_financial_v2_eps_idx "
        "ON market_financial_facts_v2(account_id, symbol, business_year, report_code)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS market_events_v2_block_idx "
        "ON market_events_pit_v2(symbol, available_at, blocked_through)"
    ),
)


@dataclass(frozen=True, slots=True)
class FinancialFact:
    symbol: str
    corp_code: str
    business_year: int
    report_code: str
    fs_div: str
    statement_division: str
    account_id: str
    account_name: str
    amount: Decimal | None
    currency: str | None
    receipt_no: str
    receipt_date: date
    available_at: str | None
    source: str
    retrieved_at: str
    payload_hash: str


def next_session_available_at(
    event_date: date, observed_sessions: Sequence[date]
) -> datetime | None:
    next_session = next(
        (session for session in sorted(set(observed_sessions)) if session > event_date),
        None,
    )
    if next_session is None:
        return None
    return datetime.combine(next_session, time(8), tzinfo=SEOUL)


def compute_ttm_eps(
    *,
    report_code: str,
    current_cumulative: Decimal | None,
    prior_annual: Decimal | None,
    prior_comparable: Decimal | None,
) -> Decimal | None:
    if current_cumulative is None:
        return None
    if report_code == "11011":
        return current_cumulative
    if report_code not in {"11013", "11012", "11014"}:
        raise ValueError(f"unsupported report code: {report_code}")
    if prior_annual is None or prior_comparable is None:
        return None
    return prior_annual + current_cumulative - prior_comparable


class OfficialApiClient:
    def __init__(
        self,
        *,
        opendart_api_key: str,
        datago_api_key: str,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not opendart_api_key or not datago_api_key:
            raise ValueError("official API keys must not be empty")
        self._dart_key = opendart_api_key
        self._datago_key = datago_api_key
        self._transport = transport or UrllibTransport()
        self._timeout = timeout

    def dart_financial_accounts(
        self, *, corp_code: str, business_year: int, report_code: str, fs_div: str
    ) -> list[dict[str, Any]]:
        if fs_div not in {"CFS", "OFS"}:
            raise ValueError("fs_div must be CFS or OFS")
        payload = self._get_json(
            f"{OPENDART_BASE_URL}/fnlttSinglAcntAll.json",
            {
                "crtfc_key": self._dart_key,
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
                "fs_div": fs_div,
            },
        )
        status = str(payload.get("status", ""))
        if status == "013":
            return []
        if status != "000":
            raise RuntimeError(f"OpenDART error {status}: {payload.get('message', '')}")
        rows = payload.get("list")
        if not isinstance(rows, list):
            raise TypeError("OpenDART list is missing")
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def dart_events(
        self, *, start: date, end: date, page: int = 1, page_count: int = 100
    ) -> dict[str, Any]:
        return self._get_json(
            f"{OPENDART_BASE_URL}/list.json",
            {
                "crtfc_key": self._dart_key,
                "bgn_de": start.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "page_no": page,
                "page_count": page_count,
            },
        )

    def dart_corporations(self) -> dict[str, str]:
        body = self._get_bytes(
            f"{OPENDART_BASE_URL}/corpCode.xml", {"crtfc_key": self._dart_key}
        )
        try:
            with ZipFile(BytesIO(body)) as archive:
                root = ET.fromstring(archive.read("CORPCODE.xml"))
        except (KeyError, ET.ParseError) as error:
            raise RuntimeError("OpenDART corporation archive is invalid") from error
        return {
            stock_code: corp_code
            for item in root.findall("list")
            if (stock_code := (item.findtext("stock_code") or "").strip())
            and (corp_code := (item.findtext("corp_code") or "").strip())
        }

    def stock_prices(
        self, *, start: date, end: date, page: int = 1, rows: int = 10_000
    ) -> dict[str, Any]:
        return self._get_json(
            DATAGO_PRICE_URL,
            {
                "serviceKey": self._datago_key,
                "resultType": "json",
                "beginBasDt": start.strftime("%Y%m%d"),
                # This endpoint treats endBasDt as an exclusive upper bound.
                "endBasDt": (end + timedelta(days=1)).strftime("%Y%m%d"),
                "pageNo": page,
                "numOfRows": rows,
            },
            encoded_keys={"serviceKey"},
        )

    def _get_json(
        self,
        base_url: str,
        query: Mapping[str, object],
        *,
        encoded_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        encoded_keys = encoded_keys or set()
        parts: list[str] = []
        for key, value in query.items():
            if key in encoded_keys:
                parts.append(f"{key}={value}")
            else:
                parts.append(urlencode({key: value}))
        request = HttpRequest("GET", f"{base_url}?{'&'.join(parts)}", {})
        response = None
        for attempt in range(3):
            try:
                response = self._transport.send(request, self._timeout)
            except OSError as error:
                if attempt == 2:
                    raise RuntimeError("official API network failure") from error
                sleep(0.5 * (attempt + 1))
                continue
            if response.status not in {429, 500, 502, 503, 504}:
                break
            if attempt < 2:
                sleep(0.5 * (attempt + 1))
        if response is None:
            raise RuntimeError("official API did not respond")
        if response.status != 200:
            raise RuntimeError(f"official API HTTP {response.status}")
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("official API returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise TypeError("official API returned invalid payload")
        return payload

    def _get_bytes(self, base_url: str, query: Mapping[str, object]) -> bytes:
        response = self._transport.send(
            HttpRequest("GET", f"{base_url}?{urlencode(query)}", {}), self._timeout
        )
        if response.status != 200:
            raise RuntimeError(f"official API HTTP {response.status}")
        return response.body


class OfficialDataRepository:
    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        for schema in (FINANCIAL_SCHEMA, EVENT_SCHEMA, UNIVERSE_SCHEMA, VALUATION_SCHEMA):
            self._connection.execute(schema)
        event_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(market_events_pit_v2)"
            ).fetchall()
        }
        if "blocked_through" not in event_columns:
            self._connection.execute(
                "ALTER TABLE market_events_pit_v2 ADD COLUMN blocked_through TEXT"
            )
        if "is_entry_blocking" not in event_columns:
            self._connection.execute(
                "ALTER TABLE market_events_pit_v2 "
                "ADD COLUMN is_entry_blocking INTEGER NOT NULL DEFAULT 0"
            )
        valuation_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(market_valuation_snapshots_v2)"
            ).fetchall()
        }
        for name in (
            "owner_equity", "listed_share_count", "bps", "reference_price",
            "trailing_per", "pbr", "bps_method",
        ):
            if name not in valuation_columns:
                self._connection.execute(
                    f"ALTER TABLE market_valuation_snapshots_v2 ADD COLUMN {name} TEXT"
                )
        for index in OFFICIAL_INDEXES:
            self._connection.execute(index)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def upsert_financial_facts(self, facts: Sequence[FinancialFact]) -> int:
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO market_financial_facts_v2 VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) ON CONFLICT(symbol, business_year, report_code, fs_div,
                              account_id, rcept_no) DO UPDATE SET
                    amount=excluded.amount, currency=excluded.currency,
                    available_at=excluded.available_at,
                    retrieved_at=excluded.retrieved_at,
                    payload_hash=excluded.payload_hash
                """,
                [
                    (
                        fact.symbol,
                        fact.corp_code,
                        fact.business_year,
                        fact.report_code,
                        fact.fs_div,
                        fact.statement_division,
                        fact.account_id,
                        fact.account_name,
                        None if fact.amount is None else str(fact.amount),
                        fact.currency,
                        fact.receipt_no,
                        fact.receipt_date.isoformat(),
                        fact.available_at,
                        fact.source,
                        fact.retrieved_at,
                        fact.payload_hash,
                    )
                    for fact in facts
                ],
            )
        return len(facts)

    def upsert_events(self, events: Sequence[Mapping[str, object]]) -> int:
        fields = (
            "symbol", "corp_code", "receipt_no", "receipt_date", "report_name",
            "available_at", "blocked_through", "is_entry_blocking",
            "is_preannounced", "scheduled_for", "source", "retrieved_at", "payload_hash",
        )
        with self._connection:
            self._connection.executemany(
                """INSERT INTO market_events_pit_v2 (
                    symbol, corp_code, rcept_no, rcept_dt, report_name,
                    available_at, blocked_through, is_entry_blocking,
                    is_preannounced, scheduled_for, source, retrieved_at, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rcept_no) DO UPDATE SET
                    report_name=excluded.report_name,
                    available_at=excluded.available_at,
                    blocked_through=excluded.blocked_through,
                    is_entry_blocking=excluded.is_entry_blocking,
                    retrieved_at=excluded.retrieved_at,
                    payload_hash=excluded.payload_hash""",
                [tuple(event[field] for field in fields) for event in events],
            )
        return len(events)

    def upsert_universe_rows(self, rows: Sequence[Mapping[str, object]]) -> int:
        fields = (
            "session_date", "symbol", "isin_code", "display_name",
            "market_category", "close_price", "market_cap", "trading_value",
            "listed_share_count", "security_type", "source", "source_record_id",
            "published_at", "available_at", "retrieved_at", "payload_hash",
        )
        with self._connection:
            self._connection.executemany(
                """INSERT INTO market_universe_raw_v2 VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) ON CONFLICT(session_date, symbol, source) DO UPDATE SET
                    close_price=excluded.close_price,
                    market_cap=excluded.market_cap,
                    trading_value=excluded.trading_value,
                    listed_share_count=excluded.listed_share_count,
                    published_at=excluded.published_at,
                    available_at=excluded.available_at,
                    retrieved_at=excluded.retrieved_at,
                    payload_hash=excluded.payload_hash""",
                [tuple(row[field] for field in fields) for row in rows],
            )
        return len(rows)

    def event_blocks_entry(self, symbol: str, decision_at: str) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM market_events_pit_v2
            WHERE symbol=? AND available_at IS NOT NULL AND available_at<=?
              AND is_entry_blocking=1
              AND blocked_through IS NOT NULL AND ?<blocked_through
            ORDER BY available_at DESC LIMIT 1""",
            (symbol, decision_at, decision_at),
        ).fetchone()
        return row is not None

    def rebuild_valuation_snapshots(self) -> int:
        eps_rows = self._connection.execute(
            """SELECT symbol, business_year, report_code, fs_div, rcept_no,
                      available_at, amount
            FROM market_financial_facts_v2
            WHERE account_id='ifrs-full_BasicEarningsLossPerShare'
              AND amount IS NOT NULL AND available_at IS NOT NULL
            ORDER BY symbol, business_year, report_code, rcept_no"""
        ).fetchall()
        records: dict[tuple[str, int, str, str], list[tuple[str, str, Decimal]]] = {}
        candidates: dict[
            tuple[str, int, str, str], tuple[str, int, str, str, str, str, Decimal]
        ] = {}
        for symbol, year, code, fs_div, receipt_no, available_at, amount in eps_rows:
            record = (
                str(symbol), int(year), str(code), str(fs_div), str(receipt_no),
                str(available_at), Decimal(str(amount)),
            )
            records.setdefault(record[:4], []).append((record[5], record[4], record[6]))
            candidate_key = (record[0], record[1], record[2], record[4])
            existing = candidates.get(candidate_key)
            if existing is None or (existing[3] != "CFS" and record[3] == "CFS"):
                candidates[candidate_key] = record
        for revisions in records.values():
            revisions.sort(key=lambda item: (item[0], item[1]))
        snapshots: list[tuple[object, ...]] = []
        for symbol, year, code, fs_div, receipt_no, available_at, current in candidates.values():
            ttm = _ttm_as_of(
                records,
                symbol=symbol,
                year=year,
                code=code,
                fs_div=fs_div,
                cutoff=available_at,
                current=current,
            )
            prior_ttm = _ttm_as_of(
                records,
                symbol=symbol,
                year=year - 1,
                code=code,
                fs_div=fs_div,
                cutoff=available_at,
            )
            growth = (
                (ttm - prior_ttm) / abs(prior_ttm)
                if ttm is not None and prior_ttm not in {None, Decimal(0)}
                else None
            )
            equity_row = self._connection.execute(
                """SELECT amount, account_id FROM market_financial_facts_v2
                WHERE symbol=? AND business_year=? AND report_code=?
                  AND fs_div=? AND rcept_no=? AND account_id IN (
                    'ifrs-full_EquityAttributableToOwnersOfParent',
                    'ifrs-full_Equity'
                  ) AND amount IS NOT NULL
                ORDER BY CASE account_id
                    WHEN 'ifrs-full_EquityAttributableToOwnersOfParent' THEN 0
                    ELSE 1 END LIMIT 1""",
                (symbol, year, code, fs_div, receipt_no),
            ).fetchone()
            market_row = self._connection.execute(
                """SELECT listed_share_count, close_price
                FROM market_universe_raw_v2
                WHERE symbol=? AND available_at IS NOT NULL AND available_at<=?
                  AND listed_share_count IS NOT NULL
                ORDER BY session_date DESC LIMIT 1""",
                (symbol, available_at),
            ).fetchone()
            equity = Decimal(str(equity_row[0])) if equity_row else None
            shares = Decimal(str(market_row[0])) if market_row else None
            price = Decimal(str(market_row[1])) if market_row and market_row[1] else None
            bps = equity / shares if equity is not None and shares not in {None, Decimal(0)} else None
            trailing_per = price / ttm if price is not None and ttm is not None and ttm > 0 else None
            pbr = price / bps if price is not None and bps is not None and bps > 0 else None
            if ttm is None:
                status = "INSUFFICIENT_HISTORY"
            elif bps is None:
                status = "VALID_EPS_ONLY"
            else:
                status = "VALID"
            snapshots.append(
                (
                    symbol, year, code, fs_div, receipt_no, available_at,
                    _decimal_text(ttm), _decimal_text(growth),
                    _decimal_text(equity), _decimal_text(shares), _decimal_text(bps),
                    _decimal_text(price), _decimal_text(trailing_per), _decimal_text(pbr),
                    (
                        f"{equity_row[1]}_PER_LISTED_SHARE"
                        if equity_row and bps is not None
                        else None
                    ),
                    status,
                    "DART_CUMULATIVE_EPS_TTM_V1",
                )
            )
        with self._connection:
            self._connection.executemany(
                """INSERT INTO market_valuation_snapshots_v2 (
                    symbol, business_year, report_code, fs_div, rcept_no,
                    available_at, ttm_eps, trailing_eps_growth_yoy,
                    owner_equity, listed_share_count, bps, reference_price,
                    trailing_per, pbr, bps_method, status, method
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) ON CONFLICT(symbol, business_year, report_code, fs_div, rcept_no)
                DO UPDATE SET available_at=excluded.available_at,
                    ttm_eps=excluded.ttm_eps,
                    trailing_eps_growth_yoy=excluded.trailing_eps_growth_yoy,
                    owner_equity=excluded.owner_equity,
                    listed_share_count=excluded.listed_share_count,
                    bps=excluded.bps, reference_price=excluded.reference_price,
                    trailing_per=excluded.trailing_per, pbr=excluded.pbr,
                    bps_method=excluded.bps_method,
                    status=excluded.status, method=excluded.method""",
                snapshots,
            )
        return len(snapshots)

    def observed_sessions(self) -> list[date]:
        rows = self._connection.execute(
            "SELECT DISTINCT session_date FROM market_universe_raw_v2 ORDER BY 1"
        ).fetchall()
        return [date.fromisoformat(row[0]) for row in rows]

    def symbols(self) -> list[str]:
        try:
            rows = self._connection.execute(
                "SELECT symbol FROM market_symbols ORDER BY symbol"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [str(row[0]) for row in rows]

    def collected_financial_keys(self) -> set[tuple[str, int, str, str]]:
        rows = self._connection.execute(
            """SELECT DISTINCT symbol, business_year, report_code, fs_div
            FROM market_financial_facts_v2"""
        ).fetchall()
        return {
            (str(symbol), int(year), str(code), str(fs_div))
            for symbol, year, code, fs_div in rows
        }

    def set_universe_availability(self, sessions: Sequence[date]) -> None:
        ordered = sorted(set(sessions))
        updates: list[tuple[str | None, str | None, str]] = []
        for index, session in enumerate(ordered):
            published_session = ordered[index + 1] if index + 1 < len(ordered) else None
            available_session = ordered[index + 2] if index + 2 < len(ordered) else None
            published_at = (
                datetime.combine(published_session, time(13), tzinfo=SEOUL).isoformat()
                if published_session
                else None
            )
            available_at = (
                datetime.combine(available_session, time(8), tzinfo=SEOUL).isoformat()
                if available_session
                else None
            )
            updates.append((published_at, available_at, session.isoformat()))
        with self._connection:
            self._connection.executemany(
                """UPDATE market_universe_raw_v2
                SET published_at=?, available_at=? WHERE session_date=?""",
                updates,
            )


class OfficialDataCollector:
    def __init__(self, client: OfficialApiClient, repository: OfficialDataRepository) -> None:
        self._client = client
        self._repository = repository

    def collect_universe(self, *, start: date, end: date) -> int:
        if end < start:
            raise ValueError("end must not precede start")
        retrieved_at = datetime.now(UTC).isoformat()
        sessions: set[date] = set()
        stored = 0
        first_payload = self._client.stock_prices(start=start, end=end, page=1)
        first_body = first_payload.get("response", {}).get("body", {})
        if "totalCount" not in first_body:
            raise RuntimeError("DataGo totalCount is missing")
        total = int(first_body["totalCount"])
        first_items = first_body.get("items", {}).get("item", [])
        if isinstance(first_items, dict):
            first_items = [first_items]
        if not isinstance(first_items, list):
            raise TypeError("DataGo stock items are missing")
        if total and not first_items:
            raise RuntimeError("DataGo first page is empty")
        page_size = len(first_items) or 10_000
        total_pages = max(1, (total + page_size - 1) // page_size)

        def store_payload(payload: Mapping[str, Any]) -> int:
            body = payload.get("response", {}).get("body", {})
            items = body.get("items", {}).get("item", [])
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                raise TypeError("DataGo stock items are missing")
            rows: list[dict[str, object]] = []
            for raw_item in items:
                if not isinstance(raw_item, Mapping) or not raw_item.get("basDt"):
                    continue
                item = dict(raw_item)
                session = _compact_date(str(item["basDt"]))
                sessions.add(session)
                raw = json.dumps(item, ensure_ascii=False, sort_keys=True).encode()
                symbol = str(item.get("srtnCd", ""))
                rows.append(
                    {
                        "session_date": session.isoformat(),
                        "symbol": symbol,
                        "isin_code": str(item.get("isinCd", "")),
                        "display_name": str(item.get("itmsNm", "")),
                        "market_category": str(item.get("mrktCtg", "")),
                        "close_price": _optional_text(item.get("clpr")),
                        "market_cap": _optional_text(item.get("mrktTotAmt")),
                        "trading_value": _optional_text(item.get("trPrc")),
                        "listed_share_count": _optional_text(item.get("lstgStCnt")),
                        "security_type": "UNKNOWN",
                        "source": "data.go.kr:GetStockPriceInfo",
                        "source_record_id": f"{session.isoformat()}:{symbol}",
                        "published_at": None,
                        "available_at": None,
                        "retrieved_at": retrieved_at,
                        "payload_hash": hashlib.sha256(raw).hexdigest(),
                    }
                )
            return self._repository.upsert_universe_rows(rows)

        stored += store_payload(first_payload)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    self._client.stock_prices,
                    start=start,
                    end=end,
                    page=page,
                    rows=page_size,
                )
                for page in range(2, total_pages + 1)
            ]
            for future in as_completed(futures):
                stored += store_payload(future.result())
        self._repository.set_universe_availability(
            self._repository.observed_sessions()
        )
        return stored

    def collect_financials(
        self, *, symbols: Sequence[str], years: Sequence[int]
    ) -> dict[str, int]:
        corporations = self._client.dart_corporations()
        sessions = self._repository.observed_sessions()
        if not sessions:
            raise RuntimeError("official market sessions must be collected first")
        facts: list[FinancialFact] = []
        stored = 0
        missing_corp = 0
        jobs: list[tuple[str, str, int, str, str]] = []
        completed_keys = self._repository.collected_financial_keys()
        for symbol in symbols:
            corp_code = corporations.get(symbol)
            if not corp_code:
                missing_corp += 1
                continue
            for year in years:
                for report_code in ("11013", "11012", "11014", "11011"):
                    for fs_div in ("CFS", "OFS"):
                        if (symbol, year, report_code, fs_div) not in completed_keys:
                            jobs.append((symbol, corp_code, year, report_code, fs_div))
        found_reports = {
            (symbol, year, code)
            for symbol, year, code, _fs_div in completed_keys
            if symbol in symbols and year in years
        }
        failed_requests = 0
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(
                    self._client.dart_financial_accounts,
                    corp_code=corp_code,
                    business_year=year,
                    report_code=report_code,
                    fs_div=fs_div,
                ): (symbol, corp_code, year, report_code, fs_div)
                for symbol, corp_code, year, report_code, fs_div in jobs
            }
            for future in as_completed(futures):
                symbol, corp_code, year, report_code, fs_div = futures[future]
                try:
                    rows = future.result()
                except RuntimeError:
                    failed_requests += 1
                    continue
                if not rows:
                    continue
                found_reports.add((symbol, year, report_code))
                facts.extend(
                    financial_facts_from_rows(
                        symbol=symbol,
                        corp_code=corp_code,
                        business_year=year,
                        report_code=report_code,
                        fs_div=fs_div,
                        rows=rows,
                        sessions=sessions,
                    )
                )
                if len(facts) >= 5_000:
                    stored += self._repository.upsert_financial_facts(facts)
                    facts.clear()
        stored += self._repository.upsert_financial_facts(facts)
        snapshots = self._repository.rebuild_valuation_snapshots()
        expected_reports = len(symbols) * len(years) * 4
        return {
            "stored": stored,
            "valuationSnapshots": snapshots,
            "missingCorporation": missing_corp,
            "missingStatement": expected_reports - len(found_reports),
            "failedRequests": failed_requests,
        }

    def collect_events(self, *, start: date, end: date) -> int:
        sessions = self._repository.observed_sessions()
        if not sessions:
            raise RuntimeError("official market sessions must be collected first")
        retrieved_at = datetime.now(UTC).isoformat()
        stored = 0
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=89))
            page = 1
            while True:
                payload = self._client.dart_events(
                    start=chunk_start, end=chunk_end, page=page
                )
                status = str(payload.get("status", ""))
                if status == "013":
                    break
                if status != "000":
                    raise RuntimeError(
                        f"OpenDART error {status}: {payload.get('message', '')}"
                    )
                items = payload.get("list", [])
                if not isinstance(items, list):
                    raise TypeError("OpenDART event list is missing")
                events: list[dict[str, object]] = []
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    receipt_text = str(item.get("rcept_dt", ""))
                    receipt_no = str(item.get("rcept_no", ""))
                    if len(receipt_text) != 8 or not receipt_no:
                        continue
                    receipt_date = _compact_date(receipt_text)
                    future_sessions = [s for s in sessions if s > receipt_date]
                    available = (
                        datetime.combine(future_sessions[0], time(8), tzinfo=SEOUL)
                        if future_sessions
                        else None
                    )
                    blocked_through = (
                        datetime.combine(future_sessions[2], time(8), tzinfo=SEOUL)
                        if len(future_sessions) >= 3
                        else None
                    )
                    report_name = str(item.get("report_nm", ""))
                    raw = json.dumps(
                        dict(item), ensure_ascii=False, sort_keys=True
                    ).encode()
                    events.append(
                        {
                            "symbol": str(item.get("stock_code", "")),
                            "corp_code": str(item.get("corp_code", "")),
                            "receipt_no": receipt_no,
                            "receipt_date": receipt_date.isoformat(),
                            "report_name": report_name,
                            "available_at": available.isoformat() if available else None,
                            "blocked_through": (
                                blocked_through.isoformat() if blocked_through else None
                            ),
                            "is_entry_blocking": int(
                                _is_entry_blocking_report(report_name)
                            ),
                            "is_preannounced": int(
                                "예고" in report_name or "안내" in report_name
                            ),
                            # list.json has no scheduled date. Never infer D-N blocking.
                            "scheduled_for": None,
                            "source": "opendart:list",
                            "retrieved_at": retrieved_at,
                            "payload_hash": hashlib.sha256(raw).hexdigest(),
                        }
                    )
                stored += self._repository.upsert_events(events)
                total_pages = int(payload.get("total_page", page))
                if page >= total_pages:
                    break
                page += 1
            chunk_start = chunk_end + timedelta(days=1)
        return stored


def financial_facts_from_rows(
    *,
    symbol: str,
    corp_code: str,
    business_year: int,
    report_code: str,
    fs_div: str,
    rows: Iterable[Mapping[str, Any]],
    sessions: Sequence[date],
    retrieved_at: datetime | None = None,
) -> list[FinancialFact]:
    retrieved = (retrieved_at or datetime.now(UTC)).isoformat()
    facts: list[FinancialFact] = []
    for row in rows:
        receipt_no = str(row.get("rcept_no", ""))
        account_id = str(row.get("account_id", ""))
        if len(receipt_no) < 8 or not account_id:
            continue
        receipt_date = _compact_date(receipt_no[:8])
        available = next_session_available_at(receipt_date, sessions)
        raw = json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode()
        facts.append(
            FinancialFact(
                symbol=symbol,
                corp_code=corp_code,
                business_year=business_year,
                report_code=report_code,
                fs_div=fs_div,
                statement_division=str(row.get("sj_div", "")),
                account_id=account_id,
                account_name=str(row.get("account_nm", "")),
                amount=_decimal_or_none(row.get("thstrm_amount")),
                currency=str(row.get("currency", "")) or None,
                receipt_no=receipt_no,
                receipt_date=receipt_date,
                available_at=available.isoformat() if available else None,
                source="opendart:fnlttSinglAcntAll",
                retrieved_at=retrieved,
                payload_hash=hashlib.sha256(raw).hexdigest(),
            )
        )
    return facts


def _decimal_or_none(value: object) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _ttm_as_of(
    records: Mapping[
        tuple[str, int, str, str], Sequence[tuple[str, str, Decimal]]
    ],
    *,
    symbol: str,
    year: int,
    code: str,
    fs_div: str,
    cutoff: str,
    current: Decimal | None = None,
) -> Decimal | None:
    if current is None:
        current = _amount_as_of(
            records, (symbol, year, code, fs_div), cutoff=cutoff
        )
    return compute_ttm_eps(
        report_code=code,
        current_cumulative=current,
        prior_annual=_amount_as_of(
            records, (symbol, year - 1, "11011", fs_div), cutoff=cutoff
        ),
        prior_comparable=_amount_as_of(
            records, (symbol, year - 1, code, fs_div), cutoff=cutoff
        ),
    )


def _amount_as_of(
    records: Mapping[
        tuple[str, int, str, str], Sequence[tuple[str, str, Decimal]]
    ],
    key: tuple[str, int, str, str],
    *,
    cutoff: str,
) -> Decimal | None:
    available = [item for item in records.get(key, ()) if item[0] <= cutoff]
    return available[-1][2] if available else None


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _compact_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").replace(tzinfo=SEOUL).date()


def _is_entry_blocking_report(report_name: str) -> bool:
    if "예고" in report_name or "안내" in report_name:
        return False
    keywords = (
        "유상증자", "무상증자", "감자", "합병", "분할", "전환사채",
        "신주인수권부사채", "잠정실적", "영업(잠정)실적",
    )
    return any(keyword in report_name for keyword in keywords)
