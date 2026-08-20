from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen as default_urlopen

from .models import SYMBOL_PATTERN

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"
KOSPI_DAILY = f"{KRX_BASE}/sto/stk_bydd_trd"
KOSDAQ_DAILY = f"{KRX_BASE}/sto/ksq_bydd_trd"


def krx_rows_to_rankings(
    kospi_rows: Sequence[Mapping[str, Any]],
    kosdaq_rows: Sequence[Mapping[str, Any]],
    *,
    count: int,
    ranked_at: datetime,
) -> dict[str, Any]:
    best: dict[str, dict[str, str]] = {}
    for row in (*kospi_rows, *kosdaq_rows):
        parsed = _parse_row(row)
        if parsed is None:
            continue
        symbol, last_price, change_rate, trading_amount = parsed
        current = best.get(symbol)
        if current is None or Decimal(trading_amount) > Decimal(
            current["tradingAmount"]
        ):
            best[symbol] = {
                "symbol": symbol,
                "lastPrice": last_price,
                "changeRate": change_rate,
                "tradingAmount": trading_amount,
            }
    ordered = sorted(
        best.values(),
        key=lambda item: (-Decimal(item["tradingAmount"]), item["symbol"]),
    )[:count]
    if not ordered:
        raise RuntimeError("krx daily rankings are empty")
    return {
        "rankedAt": ranked_at.isoformat(),
        "rankings": [
            {
                "rank": index,
                "symbol": item["symbol"],
                "tradingAmount": item["tradingAmount"],
                "price": {
                    "lastPrice": item["lastPrice"],
                    "changeRate": item["changeRate"],
                },
            }
            for index, item in enumerate(ordered, start=1)
        ],
    }


def fetch_krx_acc_trdval_rankings(
    *,
    api_key: str,
    bas_dd: str,
    count: int,
    ranked_at: datetime,
    urlopen: Callable[..., Any] = default_urlopen,
) -> dict[str, Any]:
    if not api_key.strip():
        raise RuntimeError("KRX_API_KEY is required for afternoon universe rankings")
    kospi = _fetch_block(KOSPI_DAILY, api_key=api_key, bas_dd=bas_dd, urlopen=urlopen)
    kosdaq = _fetch_block(KOSDAQ_DAILY, api_key=api_key, bas_dd=bas_dd, urlopen=urlopen)
    return krx_rows_to_rankings(kospi, kosdaq, count=count, ranked_at=ranked_at)


def _fetch_block(
    endpoint: str,
    *,
    api_key: str,
    bas_dd: str,
    urlopen: Callable[..., Any],
) -> list[Mapping[str, Any]]:
    request = Request(
        f"{endpoint}?basDd={bas_dd}",
        headers={"AUTH_KEY": api_key},
        method="GET",
    )
    try:
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as error:
        if error.code in {401, 403}:
            raise RuntimeError("krx daily rankings unauthorized") from error
        raise RuntimeError(f"krx daily rankings http {error.code}") from error
    except OSError as error:
        raise RuntimeError("krx daily rankings unavailable") from error
    if not isinstance(payload, Mapping):
        raise TypeError("krx daily rankings must be an object")
    rows = payload.get("OutBlock_1")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise TypeError("krx OutBlock_1 must be a list")
    if any(not isinstance(item, Mapping) for item in rows):
        raise TypeError("each krx row must be an object")
    return rows


def _parse_row(row: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    symbol = str(row.get("ISU_CD") or "").strip()
    if len(symbol) != 6 or not symbol.isdigit() or not SYMBOL_PATTERN.fullmatch(symbol):
        return None
    last_price = _decimal_text(row.get("TDD_CLSPRC"))
    trading_amount = _decimal_text(row.get("ACC_TRDVAL"))
    change_percent = _decimal_text(row.get("FLUC_RT"))
    if last_price is None or trading_amount is None or change_percent is None:
        return None
    change_rate = (Decimal(change_percent) / Decimal(100)).normalize()
    return symbol, last_price, format(change_rate, "f"), trading_amount


def _decimal_text(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return format(number, "f")
