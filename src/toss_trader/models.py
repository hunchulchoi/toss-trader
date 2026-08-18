from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
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
    commission: Decimal
    tax: Decimal
    reason: str
    executed_at: datetime

    @property
    def total_cost(self) -> Decimal:
        return self.commission + self.tax


@dataclass(frozen=True, slots=True)
class V2PositionPlan:
    symbol: str
    cluster_id: str
    setup_session: date
    setups: tuple[str, ...]
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    planned_heat: Decimal
    ma50: Decimal
    opened_at: datetime
    exit_pending_reason: str | None = None
    exit_triggered_at: datetime | None = None

    def __post_init__(self) -> None:
        if not SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol contains unsupported characters")
        if not self.cluster_id.strip():
            raise ValueError("cluster_id must not be empty")
        if not self.setups or any(not value.strip() for value in self.setups):
            raise ValueError("setups must not be empty")
        if min(self.quantity, self.entry_price, self.stop_price, self.ma50) <= 0:
            raise ValueError("plan quantity and prices must be positive")
        if self.stop_price >= self.entry_price:
            raise ValueError("plan stop_price must be below entry_price")
        if self.planned_heat <= 0:
            raise ValueError("plan planned_heat must be positive")
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("plan opened_at must include a timezone offset")
        if (self.exit_pending_reason is None) != (self.exit_triggered_at is None):
            raise ValueError("pending exit reason and time must be set together")
        if (
            self.exit_triggered_at is not None
            and (
                self.exit_triggered_at.tzinfo is None
                or self.exit_triggered_at.utcoffset() is None
            )
        ):
            raise ValueError("plan exit_triggered_at must include a timezone offset")
