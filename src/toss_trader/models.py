from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]+$")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol contains unsupported characters")
        if self.interval not in {"1m", "1d"}:
            raise ValueError("interval must be 1m or 1d")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("candle timestamp must include a timezone offset")
        if min(self.open_price, self.high_price, self.low_price, self.close_price) <= 0:
            raise ValueError("candle prices must be positive")
        if self.high_price < max(self.open_price, self.low_price, self.close_price):
            raise ValueError("high_price is inconsistent with OHLC values")
        if self.low_price > min(self.open_price, self.high_price, self.close_price):
            raise ValueError("low_price is inconsistent with OHLC values")
        if self.volume < 0:
            raise ValueError("volume must not be negative")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter code")


@dataclass(frozen=True, slots=True)
class TradeSignal:
    signal_id: str
    symbol: str
    side: Side
    reference_price: Decimal
    quantity: Decimal
    reason: str

    def __post_init__(self) -> None:
        if not self.signal_id.strip():
            raise ValueError("signal_id must not be empty")
        if not SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol contains unsupported characters")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not self.reason.strip():
            raise ValueError("reason must not be empty")

    @property
    def notional(self) -> Decimal:
        return self.reference_price * self.quantity


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    signal_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    notional: Decimal
    reason: str
    executed_at: datetime
