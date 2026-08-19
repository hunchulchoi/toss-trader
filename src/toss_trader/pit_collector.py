from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import sleep
from zoneinfo import ZoneInfo

from .calendar import MarketCalendarService
from .kis_flow import KisInvestorFlowCollector
from .official_data import OfficialDataCollector

SEOUL = ZoneInfo("Asia/Seoul")
KIS_FLOW_AVAILABLE_AT = time(15, 40)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PitCollectionResult:
    started_at: str
    universe_rows: int
    event_rows: int
    future_sessions: tuple[str, ...]
    flow_rows: int = 0
    flow_status: str = "UNKNOWN_NO_AUTHORIZED_SOURCE"
    flow_failures: tuple[str, ...] = ()


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
    flow_collector: KisInvestorFlowCollector | None = None,
    flow_symbols: tuple[str, ...] | list[str] = (),
) -> PitCollectionResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone offset")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    local_day = now.astimezone(SEOUL).date()
    start = local_day - timedelta(days=lookback_days)
    universe_rows = collector.collect_universe(start=start, end=local_day)
    sessions = future_kr_sessions(calendar, start=local_day, count=4)
    event_rows = collector.collect_events(
        start=start,
        end=local_day,
        additional_sessions=sessions,
        checkpoint_through=local_day - timedelta(days=1),
    )
    flow_rows = 0
    flow_status = "UNKNOWN_NO_AUTHORIZED_SOURCE"
    flow_failures: tuple[str, ...] = ()
    if flow_collector is not None:
        if not flow_symbols:
            raise ValueError("configured KIS flow collector needs symbols")
        if now.astimezone(SEOUL).time() < KIS_FLOW_AVAILABLE_AT:
            flow_status = "WAITING_FOR_KIS_1540"
        else:
            flow_rows = flow_collector.collect(
                symbols=flow_symbols,
                as_of=local_day,
                completed_through=local_day,
                retrieved_at=now.astimezone(UTC),
                calendar=calendar,
            )
            flow_failures = tuple(getattr(flow_collector, "failures", ()))
            flow_status = (
                "PARTIAL_FAILURE" if flow_failures else "AVAILABLE_FIRST_OBSERVED"
            )
    return PitCollectionResult(
        started_at=now.astimezone(UTC).isoformat(),
        universe_rows=universe_rows,
        event_rows=event_rows,
        future_sessions=tuple(session.isoformat() for session in sessions),
        flow_rows=flow_rows,
        flow_status=flow_status,
        flow_failures=flow_failures,
    )


def seconds_until_next_run(
    now: datetime,
    *,
    run_times: tuple[time, ...] = (time(0, 10), time(18, 30)),
) -> float:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone offset")
    if not run_times:
        raise ValueError("run_times must not be empty")
    local = now.astimezone(SEOUL)
    targets = sorted(
        datetime.combine(local.date(), run_at, tzinfo=SEOUL)
        for run_at in set(run_times)
    )
    target = next((candidate for candidate in targets if candidate > local), None)
    if target is None:
        target = targets[0] + timedelta(days=1)
    return (target - local).total_seconds()


def serve_pit_collector(
    run_once: Callable[[datetime], PitCollectionResult],
    *,
    once: bool = False,
    report_failure: Callable[[PitCollectionResult], None] | None = None,
) -> None:
    while True:
        result = run_once(datetime.now(UTC))
        print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
        if result.flow_failures and report_failure is not None:
            try:
                report_failure(result)
            except RuntimeError:
                logger.exception("KIS flow failure alert could not be sent")
        if once:
            return
        sleep(seconds_until_next_run(datetime.now(UTC)))
