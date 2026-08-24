from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from .calendar import MarketSession
from .models import Candle

SESSION_BAR_LIMIT = 400
SYMBOL_CAP = 15


class CandleReader(Protocol):
    def latest_candles(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[Candle]: ...


def build_market_context(
    repository: CandleReader,
    *,
    symbols: Sequence[str],
    benchmark_symbols: Sequence[str],
    session: MarketSession,
    now: datetime,
    names: Mapping[str, str] | None = None,
    max_symbols: int = SYMBOL_CAP,
    entry_arm_window: timedelta = timedelta(minutes=10),
    first_bar_offset: timedelta = timedelta(minutes=1),
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone offset")
    if not session.is_business_day or session.market_open_at is None:
        return {
            "status": "closed",
            "businessDate": session.business_date.isoformat(),
        }
    session_end = now
    if session.market_close_at is not None and session.market_close_at < now:
        session_end = session.market_close_at
    labels = names or {}
    benchmarks = tuple(dict.fromkeys(benchmark_symbols))
    watch = tuple(dict.fromkeys((*benchmarks, *symbols)))
    rows = [
        _symbol_move(
            repository,
            symbol=symbol,
            session_open=session.market_open_at,
            session_end=session_end,
            name=labels.get(symbol, symbol),
            role="benchmark" if symbol in benchmarks else "watched",
        )
        for symbol in watch
    ]
    ranked = sorted(
        (row for row in rows if row["symbol"] not in benchmarks),
        key=lambda row: (-_abs_rate(row.get("vsOpen")), str(row["symbol"])),
    )
    selected = [row for row in rows if row["symbol"] in benchmarks]
    selected.extend(ranked[: max(0, max_symbols - len(selected))])
    return {
        "status": "ok",
        "schemaVersion": 1,
        "businessDate": session.business_date.isoformat(),
        "sessionOpenAt": session.market_open_at.isoformat(),
        "observedAt": now.isoformat(),
        "sessionEndAt": session_end.isoformat(),
        "entryWindow": {
            "sessionOpenAt": session.market_open_at.isoformat(),
            "firstBarAt": (
                session.market_open_at + first_bar_offset
            ).isoformat(),
            "entryWindowCloseAt": (
                session.market_open_at + entry_arm_window
            ).isoformat(),
            "observedAt": now.isoformat(),
            "meaning": (
                "D+1 BUY only after firstBarAt and at or before entryWindowCloseAt"
            ),
        },
        "purpose": (
            "Compare skip/idle reasons with stored KR session prices. "
            "Do not invent missed buys."
        ),
        "benchmarks": [row for row in selected if row["symbol"] in benchmarks],
        "symbols": [row for row in selected if row["symbol"] not in benchmarks],
    }


def _symbol_move(
    repository: CandleReader,
    *,
    symbol: str,
    session_open: datetime,
    session_end: datetime,
    name: str,
    role: str,
) -> dict[str, Any]:
    bars = [
        candle
        for candle in repository.latest_candles(
            symbol, "1m", limit=SESSION_BAR_LIMIT
        )
        if session_open < candle.timestamp <= session_end
    ]
    daily = repository.latest_candles(symbol, "1d", limit=2)
    if not bars:
        return {
            "symbol": symbol,
            "name": name,
            "role": role,
            "coverage": "missing-1m",
            "barCount": 0,
        }
    first = bars[0]
    last = bars[-1]
    vs_open = (last.close_price / first.open_price) - Decimal(1)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "role": role,
        "coverage": "session-1m",
        "barCount": len(bars),
        "firstAt": first.timestamp.isoformat(),
        "lastAt": last.timestamp.isoformat(),
        "open": str(first.open_price),
        "last": str(last.close_price),
        "vsOpen": _rate(vs_open),
    }
    if daily:
        prev_close = daily[-1].close_price
        payload["prevClose"] = str(prev_close)
        payload["vsPrevClose"] = _rate((last.close_price / prev_close) - Decimal(1))
    return payload


def _rate(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def _abs_rate(value: object) -> Decimal:
    try:
        return abs(Decimal(str(value)))
    except Exception:
        return Decimal(0)
