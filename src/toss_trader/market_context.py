from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .calendar import country_for_symbol
from .errors import TossApiError

CONTEXT_ERRORS = (OSError, RuntimeError, TossApiError, TypeError, ValueError)


class MarketDataClient(Protocol):
    def prices(self, symbols: Sequence[str]) -> list[dict[str, Any]]: ...

    def orderbook(self, symbol: str) -> dict[str, Any]: ...

    def trades(self, symbol: str, *, count: int = 10) -> list[dict[str, Any]]: ...

    def price_limits(self, symbol: str) -> dict[str, Any]: ...

    def stock_warnings(self, symbol: str) -> list[dict[str, Any]]: ...

    def investor_trading(self, symbol: str, *, count: int = 1) -> dict[str, Any]: ...

    def program_trades(self, symbol: str, *, count: int = 1) -> dict[str, Any]: ...

    def short_selling(self, symbol: str, *, count: int = 1) -> dict[str, Any]: ...

    def credit_trades(self, symbol: str, *, count: int = 1) -> dict[str, Any]: ...

    def securities_lending(self, symbol: str, *, count: int = 1) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MarketContext:
    symbol: str
    payload: dict[str, Any]
    errors: tuple[str, ...]


class MarketContextCollector:
    def __init__(self, client: MarketDataClient) -> None:
        self._client = client

    def collect(self, symbol: str) -> MarketContext:
        payload: dict[str, Any] = {}
        errors: list[str] = []
        _put(payload, errors, "price", lambda: _price(self._client.prices((symbol,))))
        _put(payload, errors, "orderbook", lambda: _orderbook(self._client.orderbook(symbol)))
        _put(
            payload,
            errors,
            "trades",
            lambda: _trades(self._client.trades(symbol, count=10)),
        )
        _put(
            payload,
            errors,
            "priceLimits",
            lambda: _price_limits(self._client.price_limits(symbol)),
        )
        _put(
            payload,
            errors,
            "warnings",
            lambda: _warnings(self._client.stock_warnings(symbol)),
        )
        if country_for_symbol(symbol) == "KR":
            _put(
                payload,
                errors,
                "investorTrading",
                lambda: _investor(self._client.investor_trading(symbol, count=1)),
            )
            _put(
                payload,
                errors,
                "programTrades",
                lambda: _latest_record(self._client.program_trades(symbol, count=1)),
            )
            _put(
                payload,
                errors,
                "shortSelling",
                lambda: _latest_record(self._client.short_selling(symbol, count=1)),
            )
            _put(
                payload,
                errors,
                "creditTrades",
                lambda: _latest_record(self._client.credit_trades(symbol, count=1)),
            )
            _put(
                payload,
                errors,
                "securitiesLending",
                lambda: _latest_record(self._client.securities_lending(symbol, count=1)),
            )
        return MarketContext(symbol=symbol, payload=payload, errors=tuple(errors))


def _put(
    payload: dict[str, Any],
    errors: list[str],
    key: str,
    loader: Callable[[], Any],
) -> None:
    try:
        value = loader()
    except CONTEXT_ERRORS as error:
        errors.append(f"{key}: {error}")
        return
    if value is not None:
        payload[key] = value


def _price(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    row = rows[0]
    last = row.get("lastPrice")
    if last is None:
        return None
    compact: dict[str, Any] = {"lastPrice": str(last)}
    if row.get("timestamp") is not None:
        compact["timestamp"] = str(row["timestamp"])
    if row.get("currency") is not None:
        compact["currency"] = str(row["currency"])
    return compact


def _orderbook(book: Mapping[str, Any]) -> dict[str, Any] | None:
    asks = book.get("asks") if isinstance(book.get("asks"), list) else []
    bids = book.get("bids") if isinstance(book.get("bids"), list) else []
    best_ask = _extreme_price(asks, minimum=True)
    best_bid = _extreme_price(bids, minimum=False)
    if best_ask is None and best_bid is None:
        return None
    compact: dict[str, Any] = {}
    if best_ask is not None:
        compact["bestAsk"] = best_ask
    if best_bid is not None:
        compact["bestBid"] = best_bid
    return compact


def _trades(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    last = rows[0].get("price")
    volume = Decimal(0)
    for row in rows:
        raw = row.get("volume")
        if raw is None:
            continue
        try:
            volume += Decimal(str(raw))
        except InvalidOperation:
            continue
    compact: dict[str, Any] = {"count": len(rows)}
    if last is not None:
        compact["lastPrice"] = str(last)
    compact["volumeSum"] = str(volume)
    return compact


def _price_limits(limits: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    if limits.get("upperLimitPrice") is not None:
        compact["upperLimitPrice"] = str(limits["upperLimitPrice"])
    if limits.get("lowerLimitPrice") is not None:
        compact["lowerLimitPrice"] = str(limits["lowerLimitPrice"])
    return compact


def _warnings(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    types: list[str] = []
    for row in rows:
        warning = row.get("warningType")
        if warning:
            types.append(str(warning))
    return types


def _investor(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    record = _first_record(payload)
    if record is None:
        return None
    compact: dict[str, Any] = {}
    if record.get("date") is not None:
        compact["date"] = str(record["date"])
    foreigner = _net(record.get("foreigner"))
    if foreigner is not None:
        compact["foreignerNet"] = foreigner
    institution = _net(record.get("institution"))
    if institution is not None:
        compact["institutionNet"] = institution
    individual = _net(record.get("individual"))
    if individual is not None:
        compact["individualNet"] = individual
    return compact or None


def _latest_record(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    record = _first_record(payload)
    if record is None:
        return None
    return dict(record)


def _first_record(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return None
    first = records[0]
    return first if isinstance(first, Mapping) else None


def _net(group: object) -> str | None:
    if not isinstance(group, Mapping):
        return None
    value = group.get("netBuyVolume")
    return str(value) if value is not None else None


def _extreme_price(levels: Sequence[object], *, minimum: bool) -> str | None:
    prices: list[Decimal] = []
    for level in levels:
        if not isinstance(level, Mapping) or level.get("price") is None:
            continue
        try:
            prices.append(Decimal(str(level["price"])))
        except InvalidOperation:
            continue
    if not prices:
        return None
    return str(min(prices) if minimum else max(prices))
