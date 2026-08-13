from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from .advisor import create_hermes_trade_advisor
from .automation import (
    create_daily_automation_from_env,
    create_intraday_paper_automation_from_env,
    create_market_scan_automation_from_env,
    create_workflow_task_service_from_env,
    serve_automation,
)
from .calendar import MarketCalendarService
from .client import TossClient
from .config import Settings
from .cycle import PaperCycleRunner
from .cycle_state import open_cycle_state_store
from .errors import TossApiError
from .execution import PaperTradingService
from .market_data import MarketCollector, StoredMaStrategy
from .metrics import MetricsService, open_metrics_store, serve_metrics
from .models import Side, TradeSignal
from .paper import DuplicatePaperOrder, open_paper_ledger
from .portfolio import PortfolioPerformance
from .repository import open_market_repository
from .risk import RiskLimits, RiskManager, UniverseRiskContext
from .screening import MarketScanner, market_scan_to_dict
from .strategy import ma_crossover_signal
from .universe import DynamicUniverseSelector, open_universe_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toss-trader",
        description="Toss Securities read-only and paper-trading CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("config", help="show non-secret effective config")

    prices = subparsers.add_parser("prices", help="fetch current prices")
    prices.add_argument("symbols", nargs="+")

    candles = subparsers.add_parser("candles", help="fetch 1m or 1d candles")
    candles.add_argument("symbol")
    candles.add_argument("--interval", choices=("1m", "1d"), default="1m")
    candles.add_argument("--count", type=int, default=100)
    candles.add_argument("--before")
    candles.add_argument(
        "--unadjusted", action="store_true", help="disable adjusted prices"
    )

    subparsers.add_parser("accounts", help="fetch account list")

    holdings = subparsers.add_parser("holdings", help="fetch account holdings")
    holdings.add_argument("--symbol")

    collect = subparsers.add_parser(
        "collect-candles", help="fetch and persist candles idempotently"
    )
    collect.add_argument("symbol")
    collect.add_argument("--interval", choices=("1m", "1d"), default="1m")
    collect.add_argument("--count", type=int, default=100)
    collect.add_argument("--before")
    collect.add_argument("--unadjusted", action="store_true")

    stored_strategy = subparsers.add_parser(
        "scan-ma", help="evaluate MA crossover from stored candles"
    )
    stored_strategy.add_argument("symbol")
    stored_strategy.add_argument("--interval", choices=("1m", "1d"), default="1d")
    stored_strategy.add_argument("--quantity", type=Decimal, default=Decimal(1))
    stored_strategy.add_argument("--short-window", type=int, default=20)
    stored_strategy.add_argument("--long-window", type=int, default=60)

    strategy = subparsers.add_parser("ma-signal", help="evaluate MA crossover")
    strategy.add_argument("symbol")
    strategy.add_argument("closes", nargs="+", type=Decimal)
    strategy.add_argument("--quantity", type=Decimal, default=Decimal(1))
    strategy.add_argument("--short-window", type=int, default=20)
    strategy.add_argument("--long-window", type=int, default=60)
    strategy.add_argument("--as-of", type=_parse_datetime)

    paper = subparsers.add_parser(
        "paper-order", help="risk-check and record a virtual fill"
    )
    paper.add_argument("--signal-id", required=True)
    paper.add_argument("--symbol", required=True)
    paper.add_argument("--side", choices=("BUY", "SELL"), required=True)
    paper.add_argument("--price", type=Decimal, required=True)
    paper.add_argument("--quantity", type=Decimal, required=True)
    paper.add_argument("--reason", required=True)
    paper.add_argument("--market-close-at", type=_parse_datetime)
    paper.add_argument("--daily-return-rate", type=Decimal, default=Decimal(0))
    paper.add_argument("--consecutive-api-errors", type=int, default=0)

    risk_decisions = subparsers.add_parser(
        "risk-decisions", help="query persisted RiskManager audit decisions"
    )
    risk_decisions.add_argument("--limit", type=int, default=100)
    risk_decisions.add_argument("--symbol")
    risk_decisions.add_argument(
        "--status", choices=("all", "approved", "rejected"), default="all"
    )

    automation_runs = subparsers.add_parser(
        "automation-runs", help="query automation execution and Hermes token logs"
    )
    automation_runs.add_argument("--limit", type=int, default=100)
    automation_runs.add_argument(
        "--type", choices=("all", "daily", "market_scan"), default="all"
    )
    automation_runs.add_argument(
        "--status", choices=("all", "succeeded", "failed"), default="all"
    )

    cycle = subparsers.add_parser(
        "run-paper-cycle",
        help="collect watchlist, scan MA, risk-check, and paper execute",
    )
    cycle.add_argument("--symbols", nargs="+")
    cycle.add_argument("--interval", choices=("1m", "1d"))
    cycle.add_argument("--short-window", type=int)
    cycle.add_argument("--long-window", type=int)
    cycle.add_argument("--quantity", type=Decimal)
    cycle.add_argument(
        "--portfolio", choices=("legacy", "rule", "hermes"), default="legacy"
    )
    cycle.add_argument("--hermes-advisor", action="store_true")
    cycle.add_argument("--trend-entry-symbols", nargs="+")
    cycle.add_argument("--trend-entry-key")

    market_scan = subparsers.add_parser(
        "run-market-scan",
        help="analyze market benchmarks and rank discovery candidates",
    )
    market_scan.add_argument("--benchmarks", nargs="+")
    market_scan.add_argument("--symbols", nargs="+")
    market_scan.add_argument("--top-n", type=int)

    subparsers.add_parser("metrics", help="render Prometheus metrics once")
    metrics_server = subparsers.add_parser(
        "serve-metrics", help="serve Prometheus metrics over HTTP"
    )
    metrics_server.add_argument("--host")
    metrics_server.add_argument("--port", type=int)
    automation_server = subparsers.add_parser(
        "serve-automation", help="serve internal n8n paper automation API"
    )
    automation_server.add_argument("--host", default="0.0.0.0")
    automation_server.add_argument("--port", type=int, default=8088)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    try:
        if args.command == "config":
            return _emit(
                {
                    "baseUrl": settings.base_url,
                    "credentialsConfigured": bool(
                        settings.client_id and settings.client_secret
                    ),
                    "accountConfigured": bool(settings.account_seq),
                    "tradingEnabled": settings.trading_enabled,
                    "liveOrderCommandAvailable": False,
                    "paperBackend": settings.market_backend,
                    "paperDbPath": (
                        settings.paper_db_path
                        if settings.market_backend == "sqlite"
                        else None
                    ),
                    "marketBackend": settings.market_backend,
                    "databaseConfigured": settings.database_configured,
                    "databaseMissingKeys": settings.database_missing_keys,
                    "marketDbPath": (
                        settings.market_db_path
                        if settings.market_backend == "sqlite"
                        else None
                    ),
                    "watchlistSymbols": settings.watchlist_symbols,
                    "marketBenchmarkSymbols": settings.market_benchmark_symbols,
                    "discoverySymbols": settings.discovery_symbols,
                    "discoveryTopN": settings.discovery_top_n,
                    "strategyInterval": settings.strategy_interval,
                    "strategyShortWindow": settings.strategy_short_window,
                    "strategyLongWindow": settings.strategy_long_window,
                    "paperOrderQuantity": settings.paper_order_quantity,
                    "paperInitialCash": settings.paper_initial_cash,
                    "metricsHost": settings.metrics_host,
                    "metricsPort": settings.metrics_port,
                }
            )
        if args.command == "metrics":
            return _render_metrics(settings)
        if args.command == "serve-metrics":
            return _serve_metrics(settings, args)
        if args.command == "serve-automation":
            return _serve_automation(args)
        if args.command == "prices":
            return _emit(_client(settings).prices(args.symbols))
        if args.command == "candles":
            return _emit(
                _client(settings).candles(
                    args.symbol,
                    interval=args.interval,
                    count=args.count,
                    before=args.before,
                    adjusted=not args.unadjusted,
                )
            )
        if args.command == "accounts":
            return _emit(_client(settings).accounts())
        if args.command == "holdings":
            return _emit(_client(settings).holdings(args.symbol))
        if args.command == "collect-candles":
            return _collect_candles(settings, args)
        if args.command == "scan-ma":
            return _scan_ma(settings, args)
        if args.command == "ma-signal":
            result = ma_crossover_signal(
                symbol=args.symbol,
                closes=args.closes,
                as_of=args.as_of or datetime.now(UTC),
                quantity=args.quantity,
                short_window=args.short_window,
                long_window=args.long_window,
            )
            return _emit(asdict(result) if result else {"signal": None})
        if args.command == "paper-order":
            return _paper_order(settings, args)
        if args.command == "risk-decisions":
            return _risk_decisions(settings, args)
        if args.command == "automation-runs":
            return _automation_runs(settings, args)
        if args.command == "run-paper-cycle":
            return _run_paper_cycle(settings, args)
        if args.command == "run-market-scan":
            return _run_market_scan(settings, args)
    except (
        DuplicatePaperOrder,
        OSError,
        RuntimeError,
        TossApiError,
        TypeError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {"ok": False, "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


def _client(settings: Settings) -> TossClient:
    client_id, client_secret = settings.require_credentials()
    return TossClient(
        client_id=client_id,
        client_secret=client_secret,
        account_seq=settings.account_seq,
        base_url=settings.base_url,
        candle_min_interval_seconds=settings.candle_request_interval_seconds,
    )


def _paper_order(settings: Settings, args: argparse.Namespace) -> int:
    signal = TradeSignal(
        signal_id=args.signal_id,
        symbol=args.symbol,
        side=Side(args.side),
        reference_price=args.price,
        quantity=args.quantity,
        reason=args.reason,
    )
    now = datetime.now(UTC)
    ledger = open_paper_ledger(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    try:
        result = PaperTradingService(
            ledger=ledger,
            risk_manager=RiskManager(RiskLimits()),
            initial_cash=settings.paper_initial_cash,
        ).submit(
            signal,
            now=now,
            market_close_at=args.market_close_at,
            daily_return_rate=args.daily_return_rate,
            consecutive_api_errors=args.consecutive_api_errors,
        )
    finally:
        ledger.close()
    payload = {
        "approved": result.decision.approved,
        "violations": result.decision.violations,
        "fill": asdict(result.fill) if result.fill else None,
    }
    _emit(payload)
    return 0 if result.decision.approved else 2


def _risk_decisions(settings: Settings, args: argparse.Namespace) -> int:
    ledger = open_paper_ledger(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    approved = (
        None
        if args.status == "all"
        else args.status == "approved"
    )
    try:
        decisions = ledger.recent_risk_decisions(
            limit=args.limit,
            symbol=args.symbol.upper() if args.symbol else None,
            approved=approved,
        )
    finally:
        ledger.close()
    return _emit({"count": len(decisions), "decisions": decisions})


def _automation_runs(settings: Settings, args: argparse.Namespace) -> int:
    ledger = open_paper_ledger(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    try:
        runs = ledger.recent_automation_runs(
            limit=args.limit,
            run_type=None if args.type == "all" else args.type,
            status=None if args.status == "all" else args.status,
        )
    finally:
        ledger.close()
    return _emit({"count": len(runs), "runs": runs})


def _collect_candles(settings: Settings, args: argparse.Namespace) -> int:
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    try:
        result = MarketCollector(
            client=_client(settings), repository=repository
        ).collect(
            symbol=args.symbol,
            interval=args.interval,
            count=args.count,
            before=args.before,
            adjusted=not args.unadjusted,
        )
        return _emit(asdict(result))
    finally:
        repository.close()


def _scan_ma(settings: Settings, args: argparse.Namespace) -> int:
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    try:
        result = StoredMaStrategy(repository).evaluate(
            symbol=args.symbol,
            interval=args.interval,
            quantity=args.quantity,
            short_window=args.short_window,
            long_window=args.long_window,
        )
        return _emit(asdict(result) if result else {"signal": None})
    finally:
        repository.close()


def _run_paper_cycle(settings: Settings, args: argparse.Namespace) -> int:
    if args.hermes_advisor and args.portfolio != "hermes":
        raise ValueError("Hermes advisor is only valid for the hermes portfolio")
    explicit_symbols = (
        tuple(symbol.upper() for symbol in args.symbols)
        if args.symbols
        else None
    )
    interval = args.interval or settings.strategy_interval
    short_window = args.short_window or settings.strategy_short_window
    long_window = args.long_window or settings.strategy_long_window
    quantity = args.quantity or settings.paper_order_quantity
    if not 0 < short_window < long_window <= 199:
        raise ValueError("windows must satisfy 0 < short_window < long_window <= 199")
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    postgres_parameters = settings.postgres_connection_parameters()
    now = datetime.now(UTC)
    with ExitStack() as stack:
        client = _client(settings)
        market_repository = open_market_repository(
            postgres_parameters=postgres_parameters,
            sqlite_path=settings.market_db_path,
        )
        stack.callback(market_repository.close)
        paper_ledger = open_paper_ledger(
            postgres_parameters=postgres_parameters,
            sqlite_path=settings.paper_db_path,
            portfolio_id=args.portfolio,
        )
        stack.callback(paper_ledger.close)
        cycle_state = open_cycle_state_store(
            postgres_parameters=postgres_parameters,
            sqlite_path=settings.paper_db_path,
            portfolio_id=args.portfolio,
        )
        stack.callback(cycle_state.close)
        performance = PortfolioPerformance(
            ledger=paper_ledger,
            market_repository=market_repository,
        )
        universe_result = None
        if explicit_symbols is not None:
            symbols = explicit_symbols
        else:
            universe_store = open_universe_store(
                postgres_parameters=postgres_parameters,
                sqlite_path=settings.paper_db_path,
            )
            stack.callback(universe_store.close)
            latest_cycle = cycle_state.latest_run()
            universe_result = DynamicUniverseSelector(
                client=client,
                repository=market_repository,
                store=universe_store,
                risk_manager=RiskManager(RiskLimits()),
                refresh_interval=timedelta(
                    minutes=settings.dynamic_universe_refresh_minutes
                ),
                candidate_count=settings.dynamic_universe_candidate_count,
                universe_size=settings.dynamic_universe_size,
            ).resolve(
                now=now,
                held_symbols=performance.open_position_symbols(),
                risk_context=UniverseRiskContext(
                    quantity=quantity,
                    available_cash=paper_ledger.cash_balance(
                        settings.paper_initial_cash
                    ),
                    daily_return_rate=(
                        latest_cycle.daily_return_rate
                        if latest_cycle is not None
                        else Decimal(0)
                    ),
                    consecutive_api_errors=(
                        latest_cycle.consecutive_api_errors
                        if latest_cycle is not None
                        else 0
                    ),
                ),
            )
            symbols = universe_result.symbols
        result = PaperCycleRunner(
            collector=MarketCollector(client=client, repository=market_repository),
            strategy=StoredMaStrategy(market_repository),
            trading=PaperTradingService(
                ledger=paper_ledger,
                risk_manager=RiskManager(RiskLimits()),
                initial_cash=settings.paper_initial_cash,
                advisor=(
                    create_hermes_trade_advisor(
                        api_key=os.environ.get("HERMES_API_KEY", ""),
                        base_url=os.environ.get(
                            "HERMES_API_BASE_URL", "http://hermes-analysis:8642"
                        ),
                        audit=paper_ledger,
                    )
                    if args.hermes_advisor
                    else None
                ),
            ),
            calendar=MarketCalendarService(client),
            performance=performance,
            state=cycle_state,
        ).run(
            symbols=symbols,
            interval=interval,
            short_window=short_window,
            long_window=long_window,
            quantity=quantity,
            now=now,
            new_buys_allowed=(
                universe_result.new_buys_allowed
                if universe_result is not None
                else True
            ),
            trend_entry_symbols=(
                tuple(args.trend_entry_symbols)
                if args.trend_entry_symbols
                else (
                    universe_result.entry_symbols if universe_result is not None else ()
                )
            ),
            trend_entry_key=(
                args.trend_entry_key
                or (universe_result.run_id if universe_result is not None else None)
            ),
            signal_namespace=(
                args.portfolio if args.portfolio in {"rule", "hermes"} else None
            ),
        )
        cash_balance = paper_ledger.cash_balance(settings.paper_initial_cash)
    _emit(
        {
            "portfolioId": args.portfolio,
            "runId": result.run_id,
            "startedAt": result.started_at,
            "finishedAt": result.finished_at,
            "interval": result.interval,
            "dailyReturnRate": result.daily_return_rate,
            "currencyReturns": result.currency_returns,
            "initialCash": settings.paper_initial_cash,
            "cashBalance": cash_balance,
            "consecutiveApiErrors": result.consecutive_api_errors,
            "universe": (
                {
                    "runId": universe_result.run_id,
                    "refreshed": universe_result.refreshed,
                    "symbols": list(universe_result.symbols),
                    "newBuysAllowed": universe_result.new_buys_allowed,
                    "entrySymbols": list(universe_result.entry_symbols),
                }
                if universe_result is not None
                else {"source": "explicit", "symbols": list(symbols)}
            ),
            "summary": {
                "symbols": result.symbol_count,
                "signals": result.signal_count,
                "fills": result.fill_count,
                "failed": result.failed_count,
            },
            "items": [asdict(item) for item in result.items],
        }
    )
    return 3 if result.failed_count else 0


def _run_market_scan(settings: Settings, args: argparse.Namespace) -> int:
    benchmarks = (
        tuple(symbol.upper() for symbol in args.benchmarks)
        if args.benchmarks
        else settings.market_benchmark_symbols
    )
    symbols = (
        tuple(symbol.upper() for symbol in args.symbols)
        if args.symbols
        else settings.discovery_symbols
    )
    top_n = args.top_n or settings.discovery_top_n
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    try:
        result = MarketScanner(
            collector=MarketCollector(client=_client(settings), repository=repository),
            repository=repository,
        ).run(
            benchmark_symbols=benchmarks,
            discovery_symbols=symbols,
            top_n=top_n,
        )
    finally:
        repository.close()
    _emit(market_scan_to_dict(result))
    return 3 if result.errors else 0


def _render_metrics(settings: Settings) -> int:
    store = open_metrics_store(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    try:
        sys.stdout.write(
            MetricsService(store, initial_cash=settings.paper_initial_cash).render()
        )
    finally:
        store.close()
    return 0


def _serve_metrics(settings: Settings, args: argparse.Namespace) -> int:
    host = (args.host or settings.metrics_host).strip()
    port = args.port or settings.metrics_port
    if not host:
        raise ValueError("metrics host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("metrics port must be between 1 and 65535")
    store = open_metrics_store(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    try:
        service = MetricsService(store, initial_cash=settings.paper_initial_cash)
        print(
            json.dumps({"metricsServer": "listening", "host": host, "port": port}),
            flush=True,
        )
        try:
            serve_metrics(host=host, port=port, render=service.render)
        except KeyboardInterrupt:
            return 0
    finally:
        store.close()
    return 0


def _serve_automation(args: argparse.Namespace) -> int:
    host = args.host.strip()
    port = args.port
    if not host:
        raise ValueError("automation host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("automation port must be between 1 and 65535")
    service = create_daily_automation_from_env()
    intraday_service = create_intraday_paper_automation_from_env()
    market_service = create_market_scan_automation_from_env()
    print(
        json.dumps({"automationServer": "listening", "host": host, "port": port}),
        flush=True,
    )
    try:
        serve_automation(
            host=host,
            port=port,
            service=service,
            market_service=market_service,
            intraday_service=intraday_service,
            workflow_service=create_workflow_task_service_from_env(),
        )
    except KeyboardInterrupt:
        return 0
    return 0


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone offset")
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _emit(payload: Any) -> int:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )
    return 0
