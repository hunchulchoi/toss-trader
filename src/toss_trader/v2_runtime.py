from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Candle
from .repository import MarketReadRepository
from .setup_screening import SetupContextFactory
from .v2_engine import DailySetupCandidate, build_daily_candidate

SEOUL = ZoneInfo("Asia/Seoul")


class OfficialV2CycleStrategy:
    def __init__(
        self,
        repository: MarketReadRepository,
        *,
        context_factory: SetupContextFactory,
    ) -> None:
        self._repository = repository
        self._context_factory = context_factory

    def build_candidate(self, symbol: str, *, now: datetime) -> DailySetupCandidate:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("v2 cycle time must include a timezone offset")
        today = now.astimezone(SEOUL).date()
        candles = self._repository.latest_candles(symbol, "1d", limit=400)
        completed = [
            candle
            for candle in candles
            if candle.timestamp.astimezone(SEOUL).date() < today
        ]
        if len(completed) < 200:
            raise ValueError(
                f"setup-v2:missing:completed-daily-candles({len(completed)}/200)"
            )
        history = completed[-200:]
        signal_session = history[-1].timestamp.astimezone(SEOUL).date()
        context = self._context_factory(symbol, signal_session, now, False)
        return build_daily_candidate(history, context=context)

    def completed_one_minute_bars(
        self, symbol: str, *, now: datetime
    ) -> tuple[Candle, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("v2 cycle time must include a timezone offset")
        candles = self._repository.latest_candles(symbol, "1m", limit=500)
        return tuple(
            candle
            for candle in candles
            if candle.timestamp + timedelta(minutes=1) <= now
        )

    def latest_completed_daily_bar(
        self, symbol: str, *, now: datetime
    ) -> Candle | None:
        today = now.astimezone(SEOUL).date()
        candles = self._repository.latest_candles(symbol, "1d", limit=5)
        completed = [
            candle
            for candle in candles
            if candle.timestamp.astimezone(SEOUL).date() < today
        ]
        return completed[-1] if completed else None

    def cluster_id(self, symbol: str) -> str:
        del symbol
        # No trustworthy sector master is available yet. Treat every unknown
        # instrument as one conservative cluster instead of bypassing the cap.
        return "UNKNOWN"

