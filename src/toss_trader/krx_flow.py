from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .official_data import OfficialDataRepository

KRX_FLOW_SOURCE = "krx:manual-csv"
_SYMBOL = re.compile(r"^[0-9]{6}$")
SEOUL = ZoneInfo("Asia/Seoul")


class KrMarketCalendar(Protocol):
    def regular_session(self, country: str, *, now: datetime): ...


@dataclass(frozen=True, slots=True)
class KrxFlowImportResult:
    session_date: str
    target_symbols: int
    imported_symbols: int
    inserted_rows: int
    complete: bool
    missing_foreign: tuple[str, ...]
    missing_institutional: tuple[str, ...]
    missing_trading_value: tuple[str, ...]
    foreign_file_hash: str
    institutional_file_hash: str
    trading_file_hash: str | None
    available_at: str


class KrxInvestorFlowCsvImporter:
    def __init__(self, repository: OfficialDataRepository) -> None:
        self._repository = repository

    def import_files(
        self,
        *,
        session_date: date,
        foreign_csv: str | Path,
        institutional_csv: str | Path,
        trading_csv: str | Path | None = None,
        target_symbols: list[str] | tuple[str, ...],
        retrieved_at: datetime | None = None,
        session_index: int | None = None,
    ) -> KrxFlowImportResult:
        observed_at = retrieved_at or datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone offset")
        targets = tuple(sorted({symbol for symbol in target_symbols if _SYMBOL.fullmatch(symbol)}))
        if not targets:
            raise ValueError("KRX flow import needs at least one six-digit target symbol")

        foreign_bytes = Path(foreign_csv).read_bytes()
        institutional_bytes = Path(institutional_csv).read_bytes()
        trading_bytes = Path(trading_csv).read_bytes() if trading_csv else None
        foreign = _parse_net_purchase_csv(foreign_bytes)
        institutional = _parse_net_purchase_csv(institutional_bytes)
        target_set = set(targets)
        missing_foreign = tuple(sorted(target_set - foreign.keys()))
        missing_institutional = tuple(sorted(target_set - institutional.keys()))
        investor_symbols = tuple(
            sorted(target_set & foreign.keys() & institutional.keys())
        )
        if not investor_symbols:
            raise ValueError("KRX CSV files have no common target symbols")

        session_indexes = self._repository.session_indexes()
        resolved_session_index = session_indexes.get(session_date, session_index)
        if resolved_session_index is None:
            raise ValueError(f"KRX session is absent from official ledger: {session_date}")
        trading_values = self._repository.trading_values(session_date)
        if trading_bytes is not None:
            trading_values.update(_parse_trading_value_csv(trading_bytes))
        missing_trading = tuple(sorted(
            symbol
            for symbol in investor_symbols
            if symbol not in trading_values or trading_values[symbol] <= 0
        ))
        imported_symbols = tuple(
            symbol for symbol in investor_symbols if symbol not in missing_trading
        )
        if not imported_symbols:
            raise ValueError("KRX import has no target symbols with official trading value")

        existing = self._repository.flow_symbols(
            session=session_date,
            source=KRX_FLOW_SOURCE,
        )
        new_symbols = tuple(symbol for symbol in imported_symbols if symbol not in existing)

        foreign_hash = hashlib.sha256(foreign_bytes).hexdigest()
        institutional_hash = hashlib.sha256(institutional_bytes).hexdigest()
        trading_hash = hashlib.sha256(trading_bytes).hexdigest() if trading_bytes else None
        observed_text = observed_at.astimezone(UTC).isoformat()
        rows = []
        for symbol in new_symbols:
            canonical = json.dumps(
                {
                    "foreign_file_hash": foreign_hash,
                    "foreign_net_buy": str(foreign[symbol]),
                    "institutional_file_hash": institutional_hash,
                    "institutional_net_buy": str(institutional[symbol]),
                    "session_date": session_date.isoformat(),
                    "symbol": symbol,
                    "trading_value": str(trading_values[symbol]),
                    "trading_file_hash": trading_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            rows.append(
                {
                    "symbol": symbol,
                    "session_date": session_date.isoformat(),
                    "session_index": resolved_session_index,
                    "available_at": observed_text,
                    "foreign_net_buy": str(foreign[symbol]),
                    "institutional_net_buy": str(institutional[symbol]),
                    "trading_value": str(trading_values[symbol]),
                    "source": KRX_FLOW_SOURCE,
                    "source_record_id": f"{session_date.isoformat()}:{symbol}",
                    "retrieved_at": observed_text,
                    "payload_hash": hashlib.sha256(canonical).hexdigest(),
                }
            )
        inserted = self._repository.insert_flow_rows(rows)
        complete = (
            not missing_foreign and not missing_institutional and not missing_trading
        )
        if complete and inserted and set(targets) <= existing | set(new_symbols):
            self._repository.record_coverage(
                dataset="flow_krx",
                start=session_date,
                end=session_date,
                completed_at=observed_text,
                source=KRX_FLOW_SOURCE,
                row_count=len(targets),
            )
        return KrxFlowImportResult(
            session_date=session_date.isoformat(),
            target_symbols=len(targets),
            imported_symbols=len(imported_symbols),
            inserted_rows=inserted,
            complete=complete,
            missing_foreign=missing_foreign,
            missing_institutional=missing_institutional,
            missing_trading_value=missing_trading,
            foreign_file_hash=foreign_hash,
            institutional_file_hash=institutional_hash,
            trading_file_hash=trading_hash,
            available_at=observed_text,
        )


def resolve_krx_session_index(
    repository: OfficialDataRepository,
    calendar: KrMarketCalendar,
    session_date: date,
) -> int:
    indexes = repository.session_indexes()
    if session_date in indexes:
        return indexes[session_date]
    previous = max((day for day in indexes if day < session_date), default=None)
    if previous is None:
        raise ValueError("cannot resolve KRX session without an earlier official session")
    index = indexes[previous]
    candidate = previous + timedelta(days=1)
    while candidate <= session_date:
        session = calendar.regular_session(
            "KR",
            now=datetime.combine(candidate, time(12), tzinfo=SEOUL),
        )
        if session.is_business_day:
            index += 1
        if candidate == session_date:
            if not session.is_business_day:
                raise ValueError(f"KRX import date is not a Korean session: {session_date}")
            return index
        candidate += timedelta(days=1)
    raise AssertionError("unreachable")


def _parse_net_purchase_csv(raw: bytes) -> dict[str, Decimal]:
    return _parse_decimal_csv(
        raw,
        value_headers=(
            "순매수거래대금",
            "순매수대금",
            "거래대금순매수",
            "netpurchaseamount",
            "netbuyamount",
        ),
        value_name="net purchase",
    )


def _parse_trading_value_csv(raw: bytes) -> dict[str, Decimal]:
    return _parse_decimal_csv(
        raw,
        value_headers=("거래대금", "tradingvalue", "tradingamount"),
        value_name="trading value",
    )


def _parse_decimal_csv(
    raw: bytes,
    *,
    value_headers: tuple[str, ...],
    value_name: str,
) -> dict[str, Decimal]:
    text = _decode_krx_csv(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("KRX CSV header is missing")
    normalized = {_normalize_header(name): name for name in reader.fieldnames}
    symbol_key = _required_header(normalized, "종목코드", "단축코드", "symbol")
    value_key = _required_header(normalized, *value_headers)
    result: dict[str, Decimal] = {}
    for row in reader:
        raw_symbol = str(row.get(symbol_key, "")).strip().replace("'", "")
        symbol = raw_symbol.zfill(6) if raw_symbol.isdigit() else raw_symbol
        if not _SYMBOL.fullmatch(symbol):
            continue
        value = str(row.get(value_key, "")).replace(",", "").strip()
        try:
            result[symbol] = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"KRX {value_name} is invalid for {symbol}") from error
    if not result:
        raise ValueError("KRX CSV has no valid symbol rows")
    return result


def _decode_krx_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("KRX CSV must be UTF-8 or CP949")


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_()\[\]-]", "", value).lower()


def _required_header(headers: dict[str, str], *candidates: str) -> str:
    for candidate in candidates:
        match = headers.get(_normalize_header(candidate))
        if match is not None:
            return match
    raise ValueError(f"KRX CSV required column is missing: {candidates[0]}")
