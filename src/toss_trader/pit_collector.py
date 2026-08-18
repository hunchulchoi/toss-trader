from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import sleep
from zoneinfo import ZoneInfo

from .calendar import MarketCalendarService
from .official_data import OfficialDataCollector

SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class PitCollectionResult:
    started_at: str
    universe_rows: int
    event_rows: int
    future_sessions: tuple[str, ...]
    flow_status: str = "UNKNOWN_NO_AUTHORIZED_SOURCE"


def future_kr_sessions(
    calendar: MarketCalendarService,
    *,
    start: date,
    count: int = 3,
    request_pause: Callable[[float], None] = sleep,
) -> tuple[date, ...]:
    if count <= 0:
        raise ValueError("count must be positive")
    sessions: list[date] = []
    candidate = start
    for _ in range(14):
        session = calendar.regular_session(
            "KR",
            now=datetime.combine(candidate, time(12), tzinfo=SEOUL),
        )
        if session.is_business_day:
            sessions.append(candidate)
            if len(sessions) == count:
                return tuple(sessions)
        candidate += timedelta(days=1)
        request_pause(0.35)
    raise RuntimeError("Toss calendar did not return enough future KR sessions")


def run_pit_collection(
    collector: OfficialDataCollector,
    calendar: MarketCalendarService,
    *,
    now: datetime,
    lookback_days: int = 14,
) -> PitCollectionResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone offset")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    local_day = now.astimezone(SEOUL).date()
    start = local_day - timedelta(days=lookback_days)
    universe_rows = collector.collect_universe(start=start, end=local_day)
    sessions = future_kr_sessions(calendar, start=local_day + timedelta(days=1))
    event_rows = collector.collect_events(
        start=start,
        end=local_day,
        additional_sessions=sessions,
    )
    return PitCollectionResult(
        started_at=now.astimezone(UTC).isoformat(),
        universe_rows=universe_rows,
        event_rows=event_rows,
        future_sessions=tuple(session.isoformat() for session in sessions),
    )


def seconds_until_next_run(
    now: datetime,
    *,
    run_at: time = time(18, 30),
) -> float:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone offset")
    local = now.astimezone(SEOUL)
    target = datetime.combine(local.date(), run_at, tzinfo=SEOUL)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


def serve_pit_collector(
    run_once: Callable[[datetime], PitCollectionResult],
    *,
    once: bool = False,
) -> None:
    while True:
        result = run_once(datetime.now(UTC))
        print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
        if once:
            return
        sleep(seconds_until_next_run(datetime.now(UTC)))
