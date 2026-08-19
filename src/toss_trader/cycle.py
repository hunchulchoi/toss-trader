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
from .models import PaperFill, Side, TradeSignal, V2PositionPlan
from .paper import toss_trade_costs
from .portfolio import DailyPortfolioPerformance, PortfolioPerformance
from .risk import RiskDecision
from .screening import MarketRegime, analyze_market
from .setup_screening import EntryGateDecision, SetupType
from .strategy import MaCrossoverEvaluation, ma_trend_continuation_signal
from .v2_engine import (
    ADVERSE_SLIPPAGE,
    COMPLETED_ONE_MINUTE_OFFSET,
    ArmedTradePlan,
    DailySetupCandidate,
    arm_candidate,
    pullback_invalidated,
    stop_touched,
)
from .v2_runtime import OfficialV2CycleStrategy

HANDLED_CYCLE_ERRORS = (OSError, RuntimeError, TossApiError, TypeError, ValueError)


def _is_setup_v2_missing(error: BaseException) -> bool:
    return str(error).startswith("setup-v2:missing:")


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
    v2_candidates: tuple[DailySetupCandidate | None, ...] = ()


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
        return _cycle_insight(self.items, new_buys_allowed=self.snapshot.new_buys_allowed)


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
        entry_gate: Callable[[TradeSignal, datetime], EntryGateDecision] | None = None,
        v2_strategy: OfficialV2CycleStrategy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._collector = collector
        self._strategy = strategy
        self._trading = trading
        self._calendar = calendar
        self._performance = performance
        self._state = state
        self._entry_gate = entry_gate
        self._v2_strategy = v2_strategy
        self._clock = clock or (lambda: datetime.now(UTC))

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
        size = len(symbols)
        collections: list[CollectionResult | None] = [None] * size
        signals: list[TradeSignal | None] = [None] * size
        skips: list[str | None] = [None] * size
        errors: list[str | None] = [None] * size
        ma_states: list[MaCrossoverEvaluation | None] = [None] * size
        v2_candidates: list[DailySetupCandidate | None] = [None] * size
        api_failed = False

        for index, symbol in enumerate(symbols):
            try:
                collections[index] = self._collector.collect(
                    symbol=symbol,
                    interval=interval,
                    count=(
                        max(long_window + 1, 200)
                        if (
                            (self._entry_gate is not None or self._v2_strategy is not None)
                            and interval == "1d"
                        )
                        else long_window + 1
                    ),
                )
                if self._v2_strategy is not None and interval == "1m":
                    daily_collection = self._collector.collect(
                        symbol=symbol,
                        interval="1d",
                        count=200,
                    )
                    if daily_collection.next_before is not None:
                        self._collector.collect(
                            symbol=symbol,
                            interval="1d",
                            count=1,
                            before=daily_collection.next_before,
                        )
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)
                api_failed = True

        trend_entries = frozenset(trend_entry_symbols)
        for index, symbol in enumerate(symbols):
            if errors[index] is not None:
                continue
            if self._v2_strategy is not None:
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
                signals[index] = (
                    None if self._v2_strategy is not None else evaluation.signal
                )
            except InsufficientCandleHistory as error:
                skips[index] = str(error)
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)

        if interval == "1m" and self._v2_strategy is None:
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

        if self._entry_gate is not None and self._v2_strategy is None:
            for index, signal in enumerate(signals):
                if signal is None or signal.side is not Side.BUY:
                    continue
                try:
                    if interval != "1d":
                        self._collector.collect(
                            symbol=signal.symbol,
                            interval="1d",
                            count=200,
                        )
                    gate = self._entry_gate(signal, now)
                    if not gate.approved:
                        signals[index] = None
                        skips[index] = gate.reason or "setup-v2:rejected"
                except HANDLED_CYCLE_ERRORS as error:
                    errors[index] = f"setup-v2: {error}"
                    signals[index] = None
                    api_failed = True

        if self._v2_strategy is not None and interval == "1m":
            for index, symbol in enumerate(symbols):
                if errors[index] is not None:
                    continue
                try:
                    candidate = self._v2_strategy.build_candidate(symbol, now=now)
                    v2_candidates[index] = candidate
                    if not candidate.decision.approved:
                        skips[index] = _v2_rejection_reason(candidate)
                except ValueError as error:
                    if _is_setup_v2_missing(error):
                        skips[index] = str(error)
                        continue
                    errors[index] = f"setup-v2: {error}"
                except HANDLED_CYCLE_ERRORS as error:
                    errors[index] = f"setup-v2: {error}"
                    api_failed = True

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
            v2_candidates=tuple(v2_candidates),
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
        v2_candidates = list(snapshot.v2_candidates)
        rebuild_v2_candidates = len(v2_candidates) != size
        v2_plans_to_store: list[ArmedTradePlan | None] = [None] * size
        if len(ma_states) != size:
            ma_states = [None] * size
        if len(v2_candidates) != size:
            v2_candidates = [None] * size

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

        sessions: dict[str, MarketSession] = {}
        if self._v2_strategy is not None and interval == "1m":
            reserved_open_heat = Decimal(0)
            reserved_cluster_heat: dict[str, Decimal] = {}
            reserved_cash = Decimal(0)
            unplanned_positions = set(self._trading.unplanned_position_symbols())
            for country in sorted({country_for_symbol(symbol) for symbol in symbols}):
                try:
                    sessions[country] = self._calendar.regular_session(country, now=now)
                except HANDLED_CYCLE_ERRORS as error:
                    api_failed = True
                    for index, symbol in enumerate(symbols):
                        if country_for_symbol(symbol) == country and errors[index] is None:
                            errors[index] = str(error)
            for index, symbol in enumerate(symbols):
                if errors[index] is not None:
                    continue
                session = sessions.get(country_for_symbol(symbol))
                if session is None:
                    continue
                try:
                    stored_plan = self._trading.v2_position_plan(symbol)
                    entry_blocked_by_legacy = (
                        stored_plan is None and bool(unplanned_positions)
                    )
                    if (
                        rebuild_v2_candidates
                        and v2_candidates[index] is None
                        and stored_plan is None
                        and not entry_blocked_by_legacy
                    ):
                        v2_candidates[index] = self._v2_strategy.build_candidate(
                            symbol, now=now
                        )
                    if (
                        stored_plan is None
                        and v2_candidates[index] is not None
                        and not entry_blocked_by_legacy
                    ):
                        self._ensure_v2_opening_bar(
                            symbol=symbol,
                            session=session,
                            now=now,
                            collection=collections[index],
                        )
                    signal, reason, plan = self._v2_runtime_signal(
                        symbol=symbol,
                        candidate=v2_candidates[index],
                        session=session,
                        performance=performance,
                        now=now,
                        reserved_open_heat=reserved_open_heat,
                        reserved_cluster_heat=reserved_cluster_heat.get(
                            self._v2_strategy.cluster_id(symbol), Decimal(0)
                        ),
                        reserved_cash=reserved_cash,
                    )
                    signals[index] = signal
                    v2_plans_to_store[index] = plan
                    if signal is None and reason is not None:
                        skips[index] = reason
                    if signal is not None and plan is not None:
                        cluster_id = self._v2_strategy.cluster_id(symbol)
                        reserved_open_heat += plan.planned_heat
                        reserved_cluster_heat[cluster_id] = (
                            reserved_cluster_heat.get(cluster_id, Decimal(0))
                            + plan.planned_heat
                        )
                        reserved_cash += signal.notional + toss_trade_costs(signal).total
                except ValueError as error:
                    if _is_setup_v2_missing(error):
                        skips[index] = str(error)
                        continue
                    errors[index] = f"setup-v2: {error}"
                except HANDLED_CYCLE_ERRORS as error:
                    errors[index] = f"setup-v2: {error}"

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
                and self._trading.has_position(symbol)
            ):
                signals[index] = None
                already_held[index] = True
            if signals[index] is not None and signal_namespace is not None:
                signals[index] = replace(
                    signals[index],
                    signal_id=f"{signal_namespace}:{signals[index].signal_id}",
                )

        prepared_v2_plans: set[int] = set()
        if self._v2_strategy is not None:
            for index, plan in enumerate(v2_plans_to_store):
                if plan is None or signals[index] is None or errors[index] is not None:
                    continue
                try:
                    self._trading.store_v2_position_plan(
                        _persisted_v2_plan(
                            plan,
                            cluster_id=self._v2_strategy.cluster_id(plan.symbol),
                            opened_at=now,
                        )
                    )
                    prepared_v2_plans.add(index)
                except HANDLED_CYCLE_ERRORS as error:
                    errors[index] = f"setup-v2: plan persistence failed: {error}"
                    signals[index] = None

        countries = {
            country_for_symbol(symbols[index])
            for index, signal in enumerate(signals)
            if signal is not None and errors[index] is None
        }
        for country in sorted(countries):
            if country in sessions:
                continue
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
                    new_buys_allowed=new_buys_allowed,
                )
            except HANDLED_CYCLE_ERRORS as error:
                errors[index] = str(error)

        for index, execution in enumerate(executions):
            fill = execution.fill if execution is not None else None
            if index in prepared_v2_plans and fill is None:
                self._trading.clear_v2_position_plan(symbols[index])
            elif (
                fill is not None
                and fill.side is Side.SELL
                and self._v2_strategy is not None
            ):
                self._trading.clear_v2_position_plan(fill.symbol)

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
    ) -> PaperExecutionResult:
        return self._trading.submit(
            signal,
            now=now,
            market_close_at=session.market_close_at,
            market_is_business_day=session.is_business_day,
            daily_return_rate=performance.daily_return_rate,
            consecutive_api_errors=consecutive_api_errors,
            new_buys_allowed=new_buys_allowed,
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

    def _ensure_v2_opening_bar(
        self,
        *,
        symbol: str,
        session: MarketSession,
        now: datetime,
        collection: CollectionResult | None,
    ) -> None:
        assert self._v2_strategy is not None
        if not session.is_business_day or session.market_open_at is None:
            return
        target = session.market_open_at + COMPLETED_ONE_MINUTE_OFFSET
        bars = self._v2_strategy.completed_one_minute_bars(symbol, now=now)
        if any(bar.timestamp == target for bar in bars):
            return
        if not any(
            bar.timestamp.astimezone(target.tzinfo).date() == target.date()
            for bar in bars
        ):
            return

        cursor = collection.next_before if collection is not None else None
        for _ in range(3):
            if cursor is None:
                return
            page = self._collector.collect(
                symbol=symbol,
                interval="1m",
                count=200,
                before=cursor,
            )
            bars = self._v2_strategy.completed_one_minute_bars(symbol, now=now)
            if any(bar.timestamp == target for bar in bars):
                return
            if page.next_before == cursor:
                return
            cursor = page.next_before

    def _v2_runtime_signal(
        self,
        *,
        symbol: str,
        candidate: DailySetupCandidate | None,
        session: MarketSession,
        performance: DailyPortfolioPerformance,
        now: datetime,
        reserved_open_heat: Decimal,
        reserved_cluster_heat: Decimal,
        reserved_cash: Decimal,
    ) -> tuple[TradeSignal | None, str | None, ArmedTradePlan | None]:
        assert self._v2_strategy is not None
        if not session.is_business_day or session.market_open_at is None:
            return None, "setup-v2:market-closed", None
        bars = tuple(
            bar
            for bar in self._v2_strategy.completed_one_minute_bars(symbol, now=now)
            if bar.timestamp >= session.market_open_at
        )
        stored = self._trading.v2_position_plan(symbol)
        if stored is not None:
            if not self._trading.has_position(symbol):
                self._trading.clear_v2_position_plan(symbol)
                return None, "setup-v2:stale-position-plan", None
            armed = _armed_v2_plan(stored)
            if stored.exit_pending_reason is not None:
                exit_bar = next(
                    (
                        bar
                        for bar in bars
                        if stored.exit_triggered_at is not None
                        and bar.timestamp > stored.exit_triggered_at
                    ),
                    None,
                )
                if exit_bar is None:
                    return None, "setup-v2:waiting:exit-bar", None
                return (
                    _v2_sell_signal(
                        stored,
                        exit_bar.open_price,
                        reason=stored.exit_pending_reason,
                        trigger_key=stored.exit_triggered_at.isoformat(),
                    ),
                    None,
                    None,
                )

            latest_daily = self._v2_strategy.latest_completed_daily_bar(
                symbol, now=now
            )
            if (
                latest_daily is not None
                and latest_daily.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()
                > stored.setup_session
                and pullback_invalidated(
                    armed, close_price=latest_daily.close_price
                )
            ):
                if not bars:
                    return None, "setup-v2:waiting:structure-exit-bar", None
                return (
                    _v2_sell_signal(
                        stored,
                        bars[0].open_price,
                        reason="structure-invalidated",
                        trigger_key=latest_daily.timestamp.isoformat(),
                    ),
                    None,
                    None,
                )

            touched = next(
                (bar for bar in bars if stop_touched(
                    bar_low=bar.low_price, stop_price=stored.stop_price
                )),
                None,
            )
            if touched is None:
                return None, None, None
            exit_bar = next((bar for bar in bars if bar.timestamp > touched.timestamp), None)
            if exit_bar is None:
                self._trading.mark_v2_exit_pending(
                    symbol, reason="hard-stop", triggered_at=touched.timestamp
                )
                return None, "setup-v2:waiting:exit-bar", None
            return (
                _v2_sell_signal(
                    stored,
                    exit_bar.open_price,
                    reason="hard-stop",
                    trigger_key=touched.timestamp.isoformat(),
                ),
                None,
                None,
            )

        if self._trading.has_position(symbol):
            return None, "setup-v2:blocked:legacy-position-unmanaged", None
        if self._trading.unplanned_position_symbols():
            return None, "setup-v2:blocked:legacy-portfolio", None
        if candidate is None:
            return None, "setup-v2:missing:daily-candidate", None
        first_bar = next(
            (
                bar
                for bar in bars
                if bar.timestamp
                == session.market_open_at + COMPLETED_ONE_MINUTE_OFFSET
            ),
            None,
        )
        if first_bar is None:
            return None, "setup-v2:waiting:first-session-bar", None
        cluster_id = self._v2_strategy.cluster_id(symbol)
        decision = arm_candidate(
            candidate,
            first_completed_bar=first_bar,
            session_open_at=session.market_open_at,
            equity=performance.equity,
            available_cash=max(
                Decimal(0), self._trading.available_cash() - reserved_cash
            ),
            current_open_heat=self._trading.open_v2_heat() + reserved_open_heat,
            current_cluster_heat=(
                self._trading.cluster_v2_heat(cluster_id) + reserved_cluster_heat
            ),
        )
        if not decision.armed or decision.plan is None:
            return None, decision.reason, None
        plan = decision.plan
        return (
            TradeSignal(
                signal_id=(
                    f"setup-v2.2:{symbol}:{plan.setup_session.isoformat()}:entry"
                ),
                symbol=symbol,
                side=Side.BUY,
                reference_price=plan.entry_price,
                quantity=plan.quantity,
                reason="setup-v2.2 daily candidate",
            ),
            None,
            plan,
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
    if snapshot.v2_candidates and len(snapshot.v2_candidates) != size:
        raise ValueError("paper snapshot item counts do not match symbols")


def _v2_rejection_reason(candidate: DailySetupCandidate) -> str:
    parts = (
        *(f"missing:{value}" for value in candidate.decision.missing_checks),
        *(f"violation:{value}" for value in candidate.decision.violations),
    )
    return "setup-v2:" + (",".join(parts) if parts else "rejected")


def _persisted_v2_plan(
    plan: ArmedTradePlan, *, cluster_id: str, opened_at: datetime
) -> V2PositionPlan:
    return V2PositionPlan(
        symbol=plan.symbol,
        cluster_id=cluster_id,
        setup_session=plan.setup_session,
        setups=tuple(value.value for value in plan.setups),
        quantity=plan.quantity,
        entry_price=plan.entry_price,
        stop_price=plan.stop_price,
        planned_heat=plan.planned_heat,
        ma50=plan.ma50,
        signal_close=plan.signal_close,
        opened_at=opened_at,
    )


def _armed_v2_plan(plan: V2PositionPlan) -> ArmedTradePlan:
    return ArmedTradePlan(
        symbol=plan.symbol,
        quantity=plan.quantity,
        execution_open=plan.entry_price / (Decimal(1) + ADVERSE_SLIPPAGE.entry_rate),
        entry_price=plan.entry_price,
        stop_price=plan.stop_price,
        planned_heat=plan.planned_heat,
        setups=tuple(SetupType(value) for value in plan.setups),
        setup_session=plan.setup_session,
        ma50=plan.ma50,
        signal_close=plan.signal_close,
    )


def _v2_sell_signal(
    plan: V2PositionPlan,
    raw_open: Decimal,
    *,
    reason: str,
    trigger_key: str,
) -> TradeSignal:
    price = raw_open * (Decimal(1) - ADVERSE_SLIPPAGE.exit_rate)
    return TradeSignal(
        signal_id=f"setup-v2.2:{plan.symbol}:{trigger_key}:exit",
        symbol=plan.symbol,
        side=Side.SELL,
        reference_price=price,
        quantity=plan.quantity,
        reason=f"setup-v2.2 {reason}",
    )


def _error_message(items: tuple[SymbolCycleResult, ...]) -> str | None:
    errors = [f"{item.symbol}: {item.error}" for item in items if item.error]
    return "; ".join(errors) if errors else None


IDLE_PRIORITY = (
    "setup-v2-block",
    "v2-idle",
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
            ma_state=ma_state,
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
    ma_state: MaCrossoverEvaluation | None = None,
) -> str | None:
    if fill is not None:
        return None
    if error is not None:
        return "error"
    if skip_reason is not None:
        if skip_reason.startswith("setup-v2:"):
            return "setup-v2-block"
        return "insufficient-candles"
    if sell_dropped:
        return "sell-no-position"
    if already_held:
        return "already-held"
    if decision is not None and not decision.approved:
        if any(
            violation.startswith(("Hermes 거부", "Hermes 분석 실패"))
            for violation in decision.violations
        ):
            return "advisor-reject"
        return "risk-block"
    if signal is None:
        return "no-crossover" if ma_state is not None else "v2-idle"
    return None


def _cycle_insight(
    items: Sequence[SymbolCycleResult],
    *,
    new_buys_allowed: bool,
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
        "setupV2Blocked": reasons.get("setup-v2-block", 0),
        "v2Idle": reasons.get("v2-idle", 0),
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
        "skipReason": item.skip_reason,
        "error": item.error,
        "fillSide": item.fill.side.value if item.fill is not None else None,
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
