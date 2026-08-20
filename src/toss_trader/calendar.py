from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo


class CalendarClient(Protocol):
    def market_calendar(
        self, country: str, *, day: date | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MarketSession:
    country: str
    business_date: date
    is_business_day: bool
    market_open_at: datetime | None
    market_close_at: datetime | None


class MarketCalendarService:
    def __init__(self, client: CalendarClient) -> None:
        self._client = client

    def regular_session(self, country: str, *, now: datetime) -> MarketSession:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone offset")
        normalized = country.upper()
        zones = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}
        if normalized not in zones:
            raise ValueError("market calendar country must be KR or US")
        requested_day = now.astimezone(zones[normalized]).date()
        payload = self._client.market_calendar(normalized, day=requested_day)
        today = payload.get("today")
        if not isinstance(today, Mapping):
            raise TypeError("market calendar result must contain today")
        try:
            response_day = date.fromisoformat(str(today["date"]))
        except (KeyError, ValueError) as error:
            raise ValueError("market calendar today.date is invalid") from error
        if response_day != requested_day:
            raise ValueError("market calendar returned an unexpected date")

        regular = _regular_market(normalized, today)
        if regular is None:
            return MarketSession(
                country=normalized,
                business_date=response_day,
                is_business_day=False,
                market_open_at=None,
                market_close_at=None,
            )
        return MarketSession(
            country=normalized,
            business_date=response_day,
            is_business_day=True,
            market_open_at=_market_time(regular, "startTime"),
            market_close_at=_market_time(regular, "endTime"),
        )


def country_for_symbol(symbol: str) -> str:
    return "KR" if symbol.isdigit() else "US"


def previous_kr_business_date(calendar: MarketCalendarService, now: datetime) -> date:
    local = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    for delta in range(1, 14):
        day = local - timedelta(days=delta)
        probe = datetime.combine(day, time(12), tzinfo=ZoneInfo("Asia/Seoul"))
        if calendar.regular_session("KR", now=probe).is_business_day:
            return day
    raise RuntimeError("no prior KR business session")


def _regular_market(
    country: str, today: Mapping[str, object]
) -> Mapping[str, object] | None:
    if country == "KR":
        integrated = today.get("integrated")
        if integrated is None:
            return None
        if not isinstance(integrated, Mapping):
            raise TypeError("KR market calendar integrated must be an object or null")
        regular = integrated.get("regularMarket")
    else:
        regular = today.get("regularMarket")
    if regular is None:
        return None
    if not isinstance(regular, Mapping):
        raise TypeError("regularMarket must be an object or null")
    return regular


def _market_time(payload: Mapping[str, object], field: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(payload[field]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"regularMarket.{field} is invalid") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"regularMarket.{field} must include a timezone offset")
    return value
