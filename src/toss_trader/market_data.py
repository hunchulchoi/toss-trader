from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .models import Candle, TradeSignal
from .repository import MarketRepository
from .strategy import ma_crossover_signal


class CandleClient(Protocol):
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
    ) -> TradeSignal | None:
        candles = self._repository.latest_candles(
            symbol, interval, limit=long_window + 1
        )
        if len(candles) < long_window + 1:
            raise ValueError(f"need {long_window + 1} candles, found {len(candles)}")
        return ma_crossover_signal(
            symbol=symbol,
            closes=[candle.close_price for candle in candles],
            as_of=candles[-1].timestamp,
            quantity=quantity,
            short_window=short_window,
            long_window=long_window,
        )


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
