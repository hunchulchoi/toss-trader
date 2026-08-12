from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .calendar import MarketCalendarService, MarketSession, country_for_symbol
from .cycle_state import CycleStateStore
from .errors import TossApiError
from .execution import PaperExecutionResult, PaperTradingService
from .market_data import CollectionResult, MarketCollector, StoredMaStrategy
from .models import PaperFill, TradeSignal
from .portfolio import DailyPortfolioPerformance, PortfolioPerformance
from .risk import RiskDecision

HANDLED_CYCLE_ERRORS = (OSError, RuntimeError, TossApiError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class SymbolCycleResult:
    symbol: str
    collection: CollectionResult | None = None
    signal: TradeSignal | None = None
    decision: RiskDecision | None = None
    decision_id: str | None = None
    fill: PaperFill | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PaperCycleResult:
    run_id: str
    started_at: datetime
    finished_at: datetime
    interval: str
    daily_return_rate: Decimal
    currency_returns: dict[str, Decimal]
    consecutive_api_errors: int
    items: tuple[SymbolCycleResult, ...]

    @property
    def symbol_count(self) -> int:
        return len(self.items)

    @property
    def signal_count(self) -> int:
        return sum(item.signal is not None for item in self.items)

    @property
    def fill_count(self) -> int:
        return sum(item.fill is not None for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.error is not None for item in self.items)


class PaperCycleRunner:
    def __init__(
        self,
        *,
        collector: MarketCollector,
        strategy: StoredMaStrategy,
        trading: PaperTradingService,
        calendar: MarketCalendarService,
        performance: PortfolioPerformance,
        state: CycleStateStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._collector = collector
        self._strategy = strategy
        self._trading = trading
        self._calendar = calendar
        self._performance = performance
        self._state = state
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        *,
        symbols: tuple[str, ...],
        interval: str,
        short_window: int,
        long_window: int,
        quantity: Decimal,
        now: datetime,
    ) -> PaperCycleResult:
        if not symbols:
            raise ValueError("watchlist must not be empty")
        previous_api_errors = self._state.latest_consecutive_api_errors()
        run_id = self._state.start_run(
            started_at=now,
            interval=interval,
            symbol_count=len(symbols),
        )
        try:
            result = self._run_started(
                run_id=run_id,
                symbols=symbols,
                interval=interval,
                short_window=short_window,
                long_window=long_window,
                quantity=quantity,
                now=now,
                previous_api_errors=previous_api_errors,
            )
        except Exception as error:
            self._state.finish_run(
                run_id=run_id,
                finished_at=self._finished_at(now),
                status="failed",
                signal_count=0,
                fill_count=0,
                failed_count=len(symbols),
                consecutive_api_errors=previous_api_errors + 1,
                daily_return_rate=Decimal(0),
                error_message=str(error),
            )
            raise

        self._state.finish_run(
            run_id=run_id,
            finished_at=result.finished_at,
            status=_status(result),
            signal_count=result.signal_count,
            fill_count=result.fill_count,
            failed_count=result.failed_count,
            consecutive_api_errors=result.consecutive_api_errors,
            daily_return_rate=result.daily_return_rate,
            error_message=_error_message(result.items),
        )
        return result

    def _run_started(
        self,
        *,
        run_id: str,
        symbols: tuple[str, ...],
        interval: str,
        short_window: int,
        long_window: int,
        quantity: Decimal,
        now: datetime,
        previous_api_errors: int,
    ) -> PaperCycleResult:
        size = len(symbols)
        collections: list[CollectionResult | None] = [None] * size
        signals: list[TradeSignal | None] = [None] * size
        executions: list[PaperExecutionResult | None] = [None] * size
        errors: list[str | None] = [None] * size
        api_failed = False

        for index, symbol in enumerate(symbols):
            try:
                collections[index] = self._collector.collect(
                    symbol=symbol,
                    interval=interval,
                    count=long_window + 1,
                )
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)
                api_failed = True

        performance, performance_error, mark_api_failed = self._performance_for_cycle(
            symbols=symbols,
            interval=interval,
            collection_errors=errors,
        )
        api_failed = api_failed or mark_api_failed
        if performance_error is not None:
            for index in range(size):
                if errors[index] is None:
                    errors[index] = f"portfolio-risk: {performance_error}"

        for index, symbol in enumerate(symbols):
            if errors[index] is not None:
                continue
            try:
                signals[index] = self._strategy.evaluate(
                    symbol=symbol,
                    interval=interval,
                    quantity=quantity,
                    short_window=short_window,
                    long_window=long_window,
                )
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)

        sessions: dict[str, MarketSession] = {}
        countries = {
            country_for_symbol(symbols[index])
            for index, signal in enumerate(signals)
            if signal is not None and errors[index] is None
        }
        for country in sorted(countries):
            try:
                sessions[country] = self._calendar.regular_session(country, now=now)
            except HANDLED_CYCLE_ERRORS as error:
                api_failed = True
                for index, signal in enumerate(signals):
                    if (
                        signal is not None
                        and errors[index] is None
                        and country_for_symbol(symbols[index]) == country
                    ):
                        errors[index] = str(error)

        consecutive_api_errors = previous_api_errors + 1 if api_failed else 0
        for index, signal in enumerate(signals):
            if signal is None or errors[index] is not None:
                continue
            session = sessions[country_for_symbol(symbols[index])]
            try:
                executions[index] = self._execute(
                    signal,
                    now,
                    session=session,
                    performance=performance,
                    consecutive_api_errors=consecutive_api_errors,
                )
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)

        items = tuple(
            SymbolCycleResult(
                symbol=symbol,
                collection=collections[index],
                signal=signals[index],
                decision=(executions[index].decision if executions[index] else None),
                decision_id=(
                    executions[index].decision_id if executions[index] else None
                ),
                fill=executions[index].fill if executions[index] else None,
                error=errors[index],
            )
            for index, symbol in enumerate(symbols)
        )
        return PaperCycleResult(
            run_id=run_id,
            started_at=now,
            finished_at=self._finished_at(now),
            interval=interval,
            daily_return_rate=performance.daily_return_rate,
            currency_returns=performance.currency_returns,
            consecutive_api_errors=consecutive_api_errors,
            items=items,
        )

    def _performance_for_cycle(
        self,
        *,
        symbols: tuple[str, ...],
        interval: str,
        collection_errors: list[str | None],
    ) -> tuple[DailyPortfolioPerformance, str | None, bool]:
        empty = DailyPortfolioPerformance(
            daily_return_rate=Decimal(0), currency_returns={}
        )
        try:
            open_symbols = self._performance.open_position_symbols()
        except HANDLED_CYCLE_ERRORS as error:
            return empty, str(error), False

        for symbol in open_symbols:
            if symbol in symbols and interval == "1d":
                index = symbols.index(symbol)
                if collection_errors[index] is not None:
                    return empty, f"daily candle collection failed for {symbol}", True
                continue
            try:
                self._collector.collect(symbol=symbol, interval="1d", count=2)
            except HANDLED_CYCLE_ERRORS as error:
                return empty, str(error), True
        try:
            return self._performance.daily(), None, False
        except HANDLED_CYCLE_ERRORS as error:
            return empty, str(error), True

    def _execute(
        self,
        signal: TradeSignal,
        now: datetime,
        *,
        session: MarketSession,
        performance: DailyPortfolioPerformance,
        consecutive_api_errors: int,
    ) -> PaperExecutionResult:
        return self._trading.submit(
            signal,
            now=now,
            market_close_at=session.market_close_at,
            market_is_business_day=session.is_business_day,
            daily_return_rate=performance.daily_return_rate,
            consecutive_api_errors=consecutive_api_errors,
        )

    def _finished_at(self, started_at: datetime) -> datetime:
        finished_at = self._clock()
        return max(started_at, finished_at)


def _status(result: PaperCycleResult) -> str:
    if result.failed_count == 0:
        return "succeeded"
    if result.failed_count == result.symbol_count:
        return "failed"
    return "partial_failure"


def _error_message(items: tuple[SymbolCycleResult, ...]) -> str | None:
    errors = [f"{item.symbol}: {item.error}" for item in items if item.error]
    return "; ".join(errors) if errors else None
