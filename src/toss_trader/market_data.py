from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .models import Candle, TradeSignal
from .repository import MarketRepository
from .strategy import MaCrossoverEvaluation, evaluate_ma_crossover


class InsufficientCandleHistory(ValueError):
    """The request succeeded but the strategy lacks enough stored history."""


class CandleClient(Protocol):
    def stocks(self, symbols: tuple[str, ...]) -> list[dict[str, Any]]: ...

    def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int,
        before: str | None,
        adjusted: bool,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CollectionResult:
    symbol: str
    interval: str
    received: int
    upserted: int
    next_before: str | None


class MarketCollector:
    def __init__(self, *, client: CandleClient, repository: MarketRepository) -> None:
        self._client = client
        self._repository = repository

    def collect_symbol_names(self, symbols: tuple[str, ...]) -> dict[str, str]:
        requested = tuple(dict.fromkeys(symbols))
        if not requested:
            raise ValueError("symbols must not be empty")
        payload = self._client.stocks(requested)
        names: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, Mapping):
                raise TypeError("each stock must be an object")
            symbol = item.get("symbol")
            name = item.get("name")
            if not isinstance(symbol, str) or symbol not in requested:
                raise ValueError("stock response contains invalid symbol")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"stock name missing for {symbol}")
            names[symbol] = name.strip()
        missing = sorted(set(requested) - names.keys())
        if missing:
            raise ValueError(f"stock info missing symbols: {', '.join(missing)}")
        self._repository.upsert_symbol_names(names)
        return names

    def collect(
        self,
        *,
        symbol: str,
        interval: str,
        count: int = 100,
        before: str | None = None,
        adjusted: bool = True,
    ) -> CollectionResult:
        payload = self._client.candles(
            symbol,
            interval=interval,
            count=count,
            before=before,
            adjusted=adjusted,
        )
        raw_candles = payload.get("candles")
        if not isinstance(raw_candles, list):
            raise TypeError("candles response must contain a candles list")
        candles = [
            _parse_candle(symbol=symbol, interval=interval, payload=item)
            for item in raw_candles
        ]
        next_before = payload.get("nextBefore")
        if next_before is not None and not isinstance(next_before, str):
            raise ValueError("nextBefore must be a string or null")
        upserted = self._repository.upsert_candles(candles)
        return CollectionResult(
            symbol=symbol,
            interval=interval,
            received=len(candles),
            upserted=upserted,
            next_before=next_before,
        )


class StoredMaStrategy:
    def __init__(self, repository: MarketRepository) -> None:
        self._repository = repository

    def evaluate(
        self,
        *,
        symbol: str,
        interval: str,
        quantity: Decimal,
        short_window: int = 20,
        long_window: int = 60,
        allow_trend_entry: bool = False,
        entry_key: str | None = None,
    ) -> TradeSignal | None:
        return self.evaluate_state(
            symbol=symbol,
            interval=interval,
            quantity=quantity,
            short_window=short_window,
            long_window=long_window,
            allow_trend_entry=allow_trend_entry,
            entry_key=entry_key,
        ).signal

    def evaluate_state(
        self,
        *,
        symbol: str,
        interval: str,
        quantity: Decimal,
        short_window: int = 20,
        long_window: int = 60,
        allow_trend_entry: bool = False,
        entry_key: str | None = None,
    ) -> MaCrossoverEvaluation:
        candles = self._repository.latest_candles(
            symbol, interval, limit=long_window + 1
        )
        if len(candles) < long_window + 1:
            raise InsufficientCandleHistory(
                f"need {long_window + 1} candles, found {len(candles)}"
            )
        return evaluate_ma_crossover(
            symbol=symbol,
            closes=[candle.close_price for candle in candles],
            as_of=candles[-1].timestamp,
            quantity=quantity,
            short_window=short_window,
            long_window=long_window,
            allow_trend_entry=allow_trend_entry,
            entry_key=entry_key,
        )

    def latest_daily_candles(self, symbol: str) -> list[Candle]:
        return self._repository.latest_candles(symbol, "1d", limit=60)


def _parse_candle(*, symbol: str, interval: str, payload: object) -> Candle:
    if not isinstance(payload, Mapping):
        raise TypeError("each candle must be an object")
    try:
        timestamp = datetime.fromisoformat(str(payload["timestamp"]))
        currency = str(payload["currency"])
        return Candle(
            symbol=symbol,
            interval=interval,
            timestamp=timestamp,
            open_price=_decimal(payload["openPrice"]),
            high_price=_decimal(payload["highPrice"]),
            low_price=_decimal(payload["lowPrice"]),
            close_price=_decimal(payload["closePrice"]),
            volume=_decimal(payload["volume"]),
            currency=currency,
        )
    except KeyError as error:
        raise ValueError(f"candle missing field: {error.args[0]}") from error


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid decimal value: {value!r}") from error
