from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from json import dumps
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import MarketCalendarService, MarketSession, country_for_symbol
from .cycle_state import CycleStateStore
from .errors import TossApiError
from .execution import PaperExecutionResult, PaperTradingService
from .market_data import (
    CollectionResult,
    InsufficientCandleHistory,
    MarketCollector,
    StoredMaStrategy,
)
from .models import PaperFill, Side, TradeSignal
from .portfolio import DailyPortfolioPerformance, PortfolioPerformance
from .risk import RiskDecision
from .screening import MarketRegime, analyze_market
from .strategy import MaCrossoverEvaluation, ma_trend_continuation_signal

HANDLED_CYCLE_ERRORS = (OSError, RuntimeError, TossApiError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class SymbolCycleResult:
    symbol: str
    collection: CollectionResult | None = None
    signal: TradeSignal | None = None
    decision: RiskDecision | None = None
    decision_id: str | None = None
    fill: PaperFill | None = None
    skip_reason: str | None = None
    error: str | None = None
    idle_reason: str | None = None
    close_price: Decimal | None = None
    short_ma: Decimal | None = None
    long_ma: Decimal | None = None
    ma_relation: str | None = None


@dataclass(frozen=True, slots=True)
class PaperCycleSnapshot:
    evaluated_at: datetime
    symbols: tuple[str, ...]
    interval: str
    collections: tuple[CollectionResult | None, ...]
    signals: tuple[TradeSignal | None, ...]
    skips: tuple[str | None, ...]
    errors: tuple[str | None, ...]
    api_failed: bool
    new_buys_allowed: bool
    ma_states: tuple[MaCrossoverEvaluation | None, ...] = ()


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
    snapshot: PaperCycleSnapshot
    equity: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    total_costs: Decimal = Decimal(0)
    market_regime: str | None = None

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

    @property
    def skipped_count(self) -> int:
        return sum(item.skip_reason is not None for item in self.items)

    @property
    def insight(self) -> dict[str, Any]:
        return _cycle_insight(
            self.items,
            new_buys_allowed=self.snapshot.new_buys_allowed,
            market_regime=self.market_regime,
        )


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
        benchmark_symbol: str | None = None,
    ) -> None:
        self._collector = collector
        self._strategy = strategy
        self._trading = trading
        self._calendar = calendar
        self._performance = performance
        self._state = state
        self._clock = clock or (lambda: datetime.now(UTC))
        self._benchmark_symbol = benchmark_symbol

    def prepare(
        self,
        *,
        symbols: tuple[str, ...],
        interval: str,
        short_window: int,
        long_window: int,
        quantity: Decimal,
        now: datetime,
        trend_entry_symbols: tuple[str, ...] = (),
        trend_entry_key: str | None = None,
        new_buys_allowed: bool = True,
    ) -> PaperCycleSnapshot:
        if not symbols:
            raise ValueError("watchlist must not be empty")
        size = len(symbols)
        collections: list[CollectionResult | None] = [None] * size
        signals: list[TradeSignal | None] = [None] * size
        skips: list[str | None] = [None] * size
        errors: list[str | None] = [None] * size
        ma_states: list[MaCrossoverEvaluation | None] = [None] * size
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

        trend_entries = frozenset(trend_entry_symbols)
        for index, symbol in enumerate(symbols):
            if errors[index] is not None:
                continue
            try:
                evaluation = self._strategy.evaluate_state(
                    symbol=symbol,
                    interval=interval,
                    quantity=quantity,
                    short_window=short_window,
                    long_window=long_window,
                    allow_trend_entry=symbol in trend_entries,
                    entry_key=trend_entry_key,
                )
                ma_states[index] = evaluation
                signals[index] = evaluation.signal
            except InsufficientCandleHistory as error:
                skips[index] = str(error)
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)

        if interval == "1m":
            entry_key = now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            for index, symbol in enumerate(symbols):
                if errors[index] is not None or signals[index] is not None:
                    continue
                evaluation = ma_states[index]
                if evaluation is None:
                    continue
                if not self._daily_risk_on(symbol):
                    continue
                signals[index] = ma_trend_continuation_signal(
                    evaluation=evaluation,
                    symbol=symbol,
                    short_window=short_window,
                    long_window=long_window,
                    quantity=quantity,
                    entry_key=entry_key,
                )

        return PaperCycleSnapshot(
            evaluated_at=now,
            symbols=symbols,
            interval=interval,
            collections=tuple(collections),
            signals=tuple(signals),
            skips=tuple(skips),
            errors=tuple(errors),
            api_failed=api_failed,
            new_buys_allowed=new_buys_allowed,
            ma_states=tuple(ma_states),
        )

    def run(
        self,
        *,
        symbols: tuple[str, ...],
        interval: str,
        short_window: int,
        long_window: int,
        quantity: Decimal,
        now: datetime,
        new_buys_allowed: bool = True,
        trend_entry_symbols: tuple[str, ...] = (),
        trend_entry_key: str | None = None,
        signal_namespace: str | None = None,
        snapshot: PaperCycleSnapshot | None = None,
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
            prepared = snapshot or self.prepare(
                symbols=symbols,
                interval=interval,
                short_window=short_window,
                long_window=long_window,
                quantity=quantity,
                now=now,
                trend_entry_symbols=trend_entry_symbols,
                trend_entry_key=trend_entry_key,
                new_buys_allowed=new_buys_allowed,
            )
            _validate_snapshot(prepared, symbols=symbols, interval=interval, now=now)
            result = self._run_started(
                run_id=run_id,
                symbols=symbols,
                interval=interval,
                now=now,
                previous_api_errors=previous_api_errors,
                new_buys_allowed=prepared.new_buys_allowed,
                signal_namespace=signal_namespace,
                snapshot=prepared,
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
            cycle_insight=dumps(result.insight, ensure_ascii=False),
        )
        return result

    def _run_started(
        self,
        *,
        run_id: str,
        symbols: tuple[str, ...],
        interval: str,
        now: datetime,
        previous_api_errors: int,
        new_buys_allowed: bool,
        signal_namespace: str | None,
        snapshot: PaperCycleSnapshot,
    ) -> PaperCycleResult:
        size = len(symbols)
        collections = list(snapshot.collections)
        signals = list(snapshot.signals)
        skips = list(snapshot.skips)
        executions: list[PaperExecutionResult | None] = [None] * size
        errors = list(snapshot.errors)
        api_failed = snapshot.api_failed
        sell_dropped = [False] * size
        already_held = [False] * size
        ma_states = list(snapshot.ma_states)
        if len(ma_states) != size:
            ma_states = [None] * size

        performance, performance_error, mark_api_failed = self._performance_for_cycle(
            symbols=symbols,
            interval=interval,
            collection_errors=errors,
            now=now,
        )
        api_failed = api_failed or mark_api_failed
        if performance_error is not None:
            for index in range(size):
                if errors[index] is None:
                    errors[index] = f"portfolio-risk: {performance_error}"

        for index, symbol in enumerate(symbols):
            if errors[index] is not None:
                continue
            if (
                signals[index] is not None
                and signals[index].side is Side.SELL
                and not self._trading.has_position(symbol)
            ):
                signals[index] = None
                sell_dropped[index] = True
            if (
                signals[index] is not None
                and signals[index].side is Side.BUY
                and "trend continuation" in signals[index].reason
                and self._trading.has_position(symbol)
            ):
                signals[index] = None
                already_held[index] = True
            if signals[index] is not None and signal_namespace is not None:
                signals[index] = replace(
                    signals[index],
                    signal_id=f"{signal_namespace}:{signals[index].signal_id}",
                )

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
        market_regime = self._benchmark_regime()
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
                    new_buys_allowed=new_buys_allowed,
                    market_regime=market_regime,
                )
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)

        if any(execution and execution.fill for execution in executions):
            performance = self._performance.daily(now=now)

        items = tuple(
            _symbol_result(
                symbol=symbol,
                collection=collections[index],
                signal=signals[index],
                execution=executions[index],
                skip_reason=skips[index],
                error=errors[index],
                sell_dropped=sell_dropped[index],
                already_held=already_held[index],
                ma_state=ma_states[index],
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
            snapshot=snapshot,
            equity=performance.equity,
            realized_pnl=performance.realized_pnl,
            unrealized_pnl=performance.unrealized_pnl,
            total_costs=performance.total_costs,
            market_regime=market_regime,
        )

    def _performance_for_cycle(
        self,
        *,
        symbols: tuple[str, ...],
        interval: str,
        collection_errors: list[str | None],
        now: datetime,
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
            return self._performance.daily(now=now), None, False
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
        new_buys_allowed: bool,
        market_regime: str | None,
    ) -> PaperExecutionResult:
        return self._trading.submit(
            signal,
            now=now,
            market_close_at=session.market_close_at,
            market_is_business_day=session.is_business_day,
            daily_return_rate=performance.daily_return_rate,
            consecutive_api_errors=consecutive_api_errors,
            new_buys_allowed=new_buys_allowed,
            market_regime=market_regime,
        )

    def _daily_risk_on(self, symbol: str) -> bool:
        if self._daily_regime(symbol) is None:
            try:
                self._collector.collect(symbol=symbol, interval="1d", count=60)
            except HANDLED_CYCLE_ERRORS:
                return False
        return self._daily_regime(symbol) is MarketRegime.RISK_ON

    def _daily_regime(self, symbol: str) -> MarketRegime | None:
        candles = self._strategy.latest_daily_candles(symbol)
        if len(candles) < 60:
            return None
        try:
            return analyze_market(candles).regime
        except (TypeError, ValueError):
            return None

    def _benchmark_regime(self) -> str | None:
        symbol = self._benchmark_symbol
        if not symbol:
            return None
        regime = self._daily_regime(symbol)
        if regime is None:
            try:
                self._collector.collect(symbol=symbol, interval="1d", count=60)
            except HANDLED_CYCLE_ERRORS:
                return None
            regime = self._daily_regime(symbol)
        return None if regime is None else regime.value

    def _finished_at(self, started_at: datetime) -> datetime:
        finished_at = self._clock()
        return max(started_at, finished_at)


def _status(result: PaperCycleResult) -> str:
    if result.failed_count == 0:
        return "succeeded"
    if result.failed_count == result.symbol_count:
        return "failed"
    return "partial_failure"


def _validate_snapshot(
    snapshot: PaperCycleSnapshot,
    *,
    symbols: tuple[str, ...],
    interval: str,
    now: datetime,
) -> None:
    if snapshot.symbols != symbols:
        raise ValueError("paper snapshot symbols do not match cycle symbols")
    if snapshot.interval != interval:
        raise ValueError("paper snapshot interval does not match cycle interval")
    if snapshot.evaluated_at != now:
        raise ValueError("paper snapshot time does not match cycle time")
    size = len(symbols)
    if not all(
        len(values) == size
        for values in (
            snapshot.collections,
            snapshot.signals,
            snapshot.skips,
            snapshot.errors,
        )
    ):
        raise ValueError("paper snapshot item counts do not match symbols")
    if snapshot.ma_states and len(snapshot.ma_states) != size:
        raise ValueError("paper snapshot item counts do not match symbols")


def _error_message(items: tuple[SymbolCycleResult, ...]) -> str | None:
    errors = [f"{item.symbol}: {item.error}" for item in items if item.error]
    return "; ".join(errors) if errors else None


IDLE_PRIORITY = (
    "no-crossover",
    "sell-no-position",
    "already-held",
    "insufficient-candles",
    "risk-block",
    "advisor-reject",
    "error",
)


def _symbol_result(
    *,
    symbol: str,
    collection: CollectionResult | None,
    signal: TradeSignal | None,
    execution: PaperExecutionResult | None,
    skip_reason: str | None,
    error: str | None,
    sell_dropped: bool,
    already_held: bool,
    ma_state: MaCrossoverEvaluation | None,
) -> SymbolCycleResult:
    decision = execution.decision if execution else None
    fill = execution.fill if execution else None
    return SymbolCycleResult(
        symbol=symbol,
        collection=collection,
        signal=signal,
        decision=decision,
        decision_id=execution.decision_id if execution else None,
        fill=fill,
        skip_reason=skip_reason,
        error=error,
        idle_reason=_idle_reason(
            skip_reason=skip_reason,
            error=error,
            sell_dropped=sell_dropped,
            already_held=already_held,
            signal=signal,
            decision=decision,
            fill=fill,
        ),
        close_price=ma_state.close if ma_state else None,
        short_ma=ma_state.short_ma if ma_state else None,
        long_ma=ma_state.long_ma if ma_state else None,
        ma_relation=ma_state.relation if ma_state else None,
    )


def _idle_reason(
    *,
    skip_reason: str | None,
    error: str | None,
    sell_dropped: bool,
    already_held: bool,
    signal: TradeSignal | None,
    decision: RiskDecision | None,
    fill: PaperFill | None,
) -> str | None:
    if fill is not None:
        return None
    if error is not None:
        return "error"
    if skip_reason is not None:
        return "insufficient-candles"
    if sell_dropped:
        return "sell-no-position"
    if already_held:
        return "already-held"
    if decision is not None and not decision.approved:
        if any(
            violation.startswith("Hermes 거부")
            or violation.startswith("Hermes 분석 실패")
            for violation in decision.violations
        ):
            return "advisor-reject"
        return "risk-block"
    if signal is None:
        return "no-crossover"
    return None


def _cycle_insight(
    items: Sequence[SymbolCycleResult],
    *,
    new_buys_allowed: bool,
    market_regime: str | None = None,
) -> dict[str, Any]:
    reasons = Counter(
        item.idle_reason for item in items if item.idle_reason is not None
    )
    funnel = {
        "scanned": len(items),
        "evaluated": sum(
            item.error is None and item.skip_reason is None for item in items
        ),
        "skippedCandles": reasons.get("insufficient-candles", 0),
        "noCrossover": reasons.get("no-crossover", 0),
        "sellNoPosition": reasons.get("sell-no-position", 0),
        "alreadyHeld": reasons.get("already-held", 0),
        "signals": sum(item.signal is not None for item in items),
        "riskRejected": reasons.get("risk-block", 0),
        "advisorRejected": reasons.get("advisor-reject", 0),
        "fills": sum(item.fill is not None for item in items),
        "failed": sum(item.error is not None for item in items),
    }
    return {
        "idleReason": _pick_idle_reason(reasons),
        "newBuysAllowed": new_buys_allowed,
        "marketRegime": market_regime,
        "funnel": funnel,
        "reasons": dict(reasons),
        "symbols": [_symbol_insight(item) for item in items],
    }


def _pick_idle_reason(reasons: Mapping[str, int]) -> str:
    if not reasons:
        return "ok"
    highest = max(reasons.values())
    for code in IDLE_PRIORITY:
        if reasons.get(code) == highest:
            return code
    return max(reasons, key=reasons.get)


def _symbol_insight(item: SymbolCycleResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": item.symbol,
        "reason": item.idle_reason,
    }
    if item.close_price is not None:
        payload["close"] = str(item.close_price)
    if item.short_ma is not None:
        payload["maShort"] = str(item.short_ma)
    if item.long_ma is not None:
        payload["maLong"] = str(item.long_ma)
    if item.ma_relation is not None:
        payload["relation"] = item.ma_relation
    return payload
