from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from .advisor import create_hermes_trade_advisor, review_momentum_shadow_once
from .automation import (
    AlertmanagerReporter,
    _market_session_lookup_from_env,
    create_daily_automation_from_env,
    create_intraday_paper_automation_from_env,
    create_market_scan_automation_from_env,
    create_workflow_task_service_from_env,
    serve_automation,
)
from .backtest import run_ma_backtest
from .calendar import MarketCalendarService, MarketSession, previous_kr_business_date
from .client import TossClient
from .config import Settings
from .cycle import V2_ENTRY_ARM_WINDOW, PaperCycleRunner, PaperCycleSnapshot
from .cycle_funnel import aggregate_intraday_review, insights_from_runs
from .cycle_state import CycleStateStore, open_cycle_state_store
from .errors import TossApiError
from .execution import PaperTradingService
from .kis_flow import KisInvestorFlowClient, KisInvestorFlowCollector
from .krx_flow import KrxInvestorFlowCsvImporter, resolve_krx_session_index
from .krx_openapi import fetch_krx_acc_trdval_rankings
from .market_context import build_market_context
from .market_data import CollectionResult, MarketCollector, StoredMaStrategy
from .metrics import MetricsService, open_metrics_store, serve_metrics
from .models import Side, TradeSignal
from .momentum_shadow import (
    RULE_VERSION as MOMENTUM_SHADOW_RULE_VERSION,
)
from .momentum_shadow import (
    evaluate_momentum_shadow,
)
from .momentum_shadow import (
    ranking_symbols as momentum_ranking_symbols,
)
from .official_data import (
    OfficialApiClient,
    OfficialDataCollector,
    OfficialDataRepository,
    open_official_data_repository,
)
from .paper import DuplicatePaperOrder, PaperLedgerStore, open_paper_ledger
from .paper_mcp import PaperMcpService, PostgresPaperReadStore, serve_paper_mcp
from .paper_timeline import PostgresPaperTimelineStore
from .pit_collector import run_pit_collection, serve_pit_collector
from .portfolio import PortfolioPerformance
from .portfolio_backtest import PortfolioBacktestResult, run_ma_portfolio_backtest
from .repository import MarketRepository, open_market_repository
from .risk import (
    N8nRiskManager,
    RiskDecision,
    RiskLimits,
    RiskManager,
    UniverseRiskContext,
)
from .setup_screening import OfficialSetupContextFactory
from .strategy import MaCrossoverEvaluation, ma_crossover_signal
from .timeline_web import serve_timeline
from .universe import (
    RANKING_SOURCE_OPENING,
    DynamicUniverseSelector,
    open_universe_store,
)
from .v2_engine import COMPLETED_ONE_MINUTE_OFFSET
from .v2_runtime import OfficialV2CycleStrategy
from .v2_screening import V2MarketScanner, v2_market_scan_to_dict
from .walk_forward import WalkForwardResult, run_ma_walk_forward

INTRADAY_SAMPLE_CANDLE_COUNT = 30
SESSION_MINUTE_FETCH_COUNT = 200
MOMENTUM_SAMPLE_LIMIT = 30


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

    intraday_backfill = subparsers.add_parser(
        "backfill-intraday-samples",
        help="restore available 1m candles for observed universe candidates",
    )
    intraday_backfill.add_argument("--as-of", type=date.fromisoformat)
    intraday_backfill.add_argument("--symbols", nargs="+")
    intraday_backfill.add_argument("--max-eligible-rank", type=int, default=30)

    official = subparsers.add_parser(
        "collect-official-data",
        help="collect fail-closed PIT data from DataGo and OpenDART",
    )
    official.add_argument("kind", choices=("universe", "events", "financials", "all"))
    official.add_argument("--start", type=date.fromisoformat)
    official.add_argument("--end", type=date.fromisoformat)
    official.add_argument("--symbols", nargs="+")
    official.add_argument("--years", nargs="+", type=int)

    pit_collector = subparsers.add_parser(
        "serve-pit-collector",
        help="collect official PIT events daily; flow stays UNKNOWN without a source",
    )
    pit_collector.add_argument("--once", action="store_true")
    pit_collector.add_argument("--lookback-days", type=int, default=14)

    kis_flow = subparsers.add_parser(
        "collect-kis-flow",
        help="collect first-observed KIS per-symbol investor flow",
    )
    kis_flow.add_argument("--symbols", nargs="+")
    kis_flow.add_argument("--as-of", type=date.fromisoformat)
    kis_flow.add_argument("--completed-through", type=date.fromisoformat)

    krx_flow = subparsers.add_parser(
        "import-krx-flow-csv",
        help="import first-observed official KRX investor flow CSV files",
    )
    krx_flow.add_argument("--session-date", required=True, type=date.fromisoformat)
    krx_flow.add_argument("--foreign-csv", required=True)
    krx_flow.add_argument("--institutional-csv", required=True)
    krx_flow.add_argument("--trading-csv")

    migrate_pit = subparsers.add_parser(
        "migrate-official-sqlite",
        help="copy legacy SQLite PIT rows into PostgreSQL idempotently",
    )
    migrate_pit.add_argument("--source", required=True)

    sync_sectors = subparsers.add_parser(
        "sync-symbol-sectors",
        help="sync sector cluster_id for market symbols from OpenDART",
    )
    sync_sectors.add_argument("--symbols", nargs="+")

    stored_strategy = subparsers.add_parser(
        "scan-ma", help="evaluate MA crossover from stored candles"
    )
    stored_strategy.add_argument("symbol")
    stored_strategy.add_argument("--interval", choices=("1m", "1d"), default="1d")
    stored_strategy.add_argument("--quantity", type=Decimal, default=Decimal(1))
    stored_strategy.add_argument("--short-window", type=int, default=20)
    stored_strategy.add_argument("--long-window", type=int, default=60)

    backtest = subparsers.add_parser(
        "backtest-ma", help="backtest MA crossover from stored candles"
    )
    backtest.add_argument("symbol")
    backtest.add_argument("--interval", choices=("1m", "1d"), default="1d")
    backtest.add_argument("--count", type=int, default=1000)
    backtest.add_argument("--quantity", type=Decimal, default=Decimal(1))
    backtest.add_argument("--initial-cash", type=Decimal)
    backtest.add_argument("--short-window", type=int, default=20)
    backtest.add_argument("--long-window", type=int, default=60)
    backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))

    portfolio_backtest = subparsers.add_parser(
        "backtest-portfolio-ma",
        help="backtest MA crossover across symbols with shared cash",
    )
    portfolio_backtest.add_argument("symbols", nargs="+")
    portfolio_backtest.add_argument("--interval", choices=("1m", "1d"), default="1d")
    portfolio_backtest.add_argument("--count", type=int, default=1000)
    portfolio_backtest.add_argument("--quantity", type=Decimal, default=Decimal(1))
    portfolio_backtest.add_argument("--initial-cash", type=Decimal)
    portfolio_backtest.add_argument("--short-window", type=int, default=20)
    portfolio_backtest.add_argument("--long-window", type=int, default=60)
    portfolio_backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))
    portfolio_backtest.add_argument("--max-open-positions", type=int)
    portfolio_backtest.add_argument("--max-daily-buys", type=int)
    portfolio_backtest.add_argument("--max-position-notional", type=Decimal)
    portfolio_backtest.add_argument("--max-order-notional", type=Decimal)
    portfolio_backtest.add_argument(
        "--format", choices=("json", "csv"), default="json", dest="output_format"
    )

    timeline = subparsers.add_parser(
        "serve-paper-timeline",
        help="serve separate Rule and Hermes paper-ledger timelines",
    )
    timeline.add_argument("--host", default="127.0.0.1")
    timeline.add_argument("--port", type=int, default=8091)

    walk_forward = subparsers.add_parser(
        "walk-forward-ma", help="rank MA parameters on train and holdout data"
    )
    walk_forward.add_argument("symbol")
    walk_forward.add_argument("--interval", choices=("1m", "1d"), default="1d")
    walk_forward.add_argument("--count", type=int, default=1000)
    walk_forward.add_argument(
        "--short-windows", nargs="+", type=int, default=(5, 10, 20)
    )
    walk_forward.add_argument(
        "--long-windows", nargs="+", type=int, default=(20, 40, 60)
    )
    walk_forward.add_argument("--train-ratio", type=Decimal, default=Decimal("0.7"))
    walk_forward.add_argument("--quantity", type=Decimal, default=Decimal(1))
    walk_forward.add_argument("--initial-cash", type=Decimal)
    walk_forward.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))
    walk_forward.add_argument(
        "--format", choices=("json", "csv"), default="json", dest="output_format"
    )

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
        "--type",
        choices=("all", "daily", "market_scan", "hermes_trade", "n8n_flow"),
        default="all",
    )
    automation_runs.add_argument(
        "--status",
        choices=("all", "succeeded", "failed", "skipped"),
        default="all",
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
    cycle.add_argument("--snapshot-stdin", action="store_true")

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
    paper_mcp_server = subparsers.add_parser(
        "serve-paper-mcp", help="serve internal read-only paper ledger MCP"
    )
    paper_mcp_server.add_argument("--host", default="0.0.0.0")
    paper_mcp_server.add_argument("--port", type=int, default=8090)
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
        if args.command == "serve-paper-mcp":
            return _serve_paper_mcp(settings, args)
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
        if args.command == "backfill-intraday-samples":
            return _backfill_intraday_samples(settings, args)
        if args.command == "collect-official-data":
            return _collect_official_data(settings, args)
        if args.command == "serve-pit-collector":
            return _serve_pit_collector(settings, args)
        if args.command == "collect-kis-flow":
            return _collect_kis_flow(settings, args)
        if args.command == "import-krx-flow-csv":
            return _import_krx_flow_csv(settings, args)
        if args.command == "migrate-official-sqlite":
            return _migrate_official_sqlite(settings, args)
        if args.command == "sync-symbol-sectors":
            return _sync_symbol_sectors(settings, args)
        if args.command == "scan-ma":
            return _scan_ma(settings, args)
        if args.command == "backtest-ma":
            return _backtest_ma(settings, args)
        if args.command == "backtest-portfolio-ma":
            return _backtest_portfolio_ma(settings, args)
        if args.command == "serve-paper-timeline":
            return _serve_paper_timeline(settings, args)
        if args.command == "walk-forward-ma":
            return _walk_forward_ma(settings, args)
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


def _collect_official_data(settings: Settings, args: argparse.Namespace) -> int:
    dart_key = os.environ.get("OPENDART_API_KEY", "")
    datago_key = os.environ.get("DATAGOKR_API_KEY", "")
    if not dart_key or not datago_key:
        raise ValueError("OPENDART_API_KEY and DATAGOKR_API_KEY are required")
    end = args.end or datetime.now(UTC).date()
    start = args.start or end - timedelta(days=730)
    years = args.years or list(range(start.year - 1, end.year + 1))
    repository = _official_repository(settings)
    try:
        collector = OfficialDataCollector(
            OfficialApiClient(
                opendart_api_key=dart_key,
                datago_api_key=datago_key,
            ),
            repository,
        )
        result: dict[str, Any] = {}
        if args.kind in {"universe", "all"}:
            result["universeRows"] = collector.collect_universe(start=start, end=end)
        if args.kind in {"events", "all"}:
            result["eventRows"] = collector.collect_events(start=start, end=end)
        if args.kind in {"financials", "all"}:
            symbols = args.symbols or repository.symbols()
            if not symbols:
                raise ValueError(
                    "financial collection needs --symbols or market_symbols"
                )
            result["financialFacts"] = collector.collect_financials(
                symbols=symbols,
                years=years,
            )
        result["safety"] = {
            "tradingEnabled": False,
            "securityType": "UNKNOWN until official instrument master is authorized",
            "valuationMultiplier": "1.0 until derived snapshots pass audit",
        }
        return _emit(result)
    finally:
        repository.close()


def _serve_pit_collector(settings: Settings, args: argparse.Namespace) -> int:
    dart_key = os.environ.get("OPENDART_API_KEY", "")
    datago_key = os.environ.get("DATAGOKR_API_KEY", "")
    if not dart_key or not datago_key:
        raise ValueError("OPENDART_API_KEY and DATAGOKR_API_KEY are required")
    kis_key, kis_secret = _kis_credentials()
    repository = _official_repository(settings)
    try:
        collector = OfficialDataCollector(
            OfficialApiClient(
                opendart_api_key=dart_key,
                datago_api_key=datago_key,
            ),
            repository,
        )
        calendar = MarketCalendarService(_client(settings))
        flow_collector = KisInvestorFlowCollector(
            KisInvestorFlowClient(
                app_key=kis_key,
                app_secret=kis_secret,
                base_url=os.environ.get(
                    "KIS_API_BASE_URL",
                    "https://openapi.koreainvestment.com:9443",
                ),
            ),
            repository,
        )
        failure_reporter = AlertmanagerReporter(
            url=os.environ.get(
                "ALERTMANAGER_API_URL", "http://alertmanager:9093/api/v2/alerts"
            ),
            alert_name="TossTraderKisFlowFailure",
            summary="Toss Trader KIS 수급 수집 실패",
        )
        serve_pit_collector(
            lambda now: run_pit_collection(
                collector,
                calendar,
                now=now,
                lookback_days=args.lookback_days,
                flow_collector=flow_collector,
                flow_symbols=repository.flow_collection_symbols(),
            ),
            once=args.once,
            report_failure=lambda result: failure_reporter.report(
                {
                    "ok": False,
                    "severity": "critical",
                    "stage": "kis-flow",
                    "analysis": "KIS 수급 수집 실패\n"
                    + "\n".join(result.flow_failures[:5]),
                }
            ),
        )
    finally:
        repository.close()
    return 0


def _collect_kis_flow(settings: Settings, args: argparse.Namespace) -> int:
    key, secret = _kis_credentials()
    repository = _official_repository(settings)
    try:
        symbols = args.symbols or repository.flow_collection_symbols()
        if not symbols:
            raise ValueError("KIS flow collection needs --symbols or market_symbols")
        now = datetime.now(UTC)
        as_of = args.as_of or now.astimezone().date()
        completed_through = args.completed_through or as_of - timedelta(days=1)
        rows = KisInvestorFlowCollector(
            KisInvestorFlowClient(
                app_key=key,
                app_secret=secret,
                base_url=os.environ.get(
                    "KIS_API_BASE_URL",
                    "https://openapi.koreainvestment.com:9443",
                ),
            ),
            repository,
        ).collect(
            symbols=symbols,
            as_of=as_of,
            completed_through=completed_through,
            retrieved_at=now,
            calendar=MarketCalendarService(_client(settings)),
        )
        return _emit(
            {
                "flowRows": rows,
                "symbols": len(symbols),
                "completedThrough": completed_through,
                "source": "KIS FHPTJ04160001",
                "tradingEnabled": False,
            }
        )
    finally:
        repository.close()


def _import_krx_flow_csv(settings: Settings, args: argparse.Namespace) -> int:
    repository = _official_repository(settings)
    try:
        session_index = resolve_krx_session_index(
            repository,
            MarketCalendarService(_client(settings)),
            args.session_date,
        )
        result = KrxInvestorFlowCsvImporter(repository).import_files(
            session_date=args.session_date,
            foreign_csv=args.foreign_csv,
            institutional_csv=args.institutional_csv,
            trading_csv=args.trading_csv,
            session_index=session_index,
        )
        return _emit({**asdict(result), "tradingEnabled": False})
    finally:
        repository.close()


def _official_repository(settings: Settings) -> OfficialDataRepository:
    return open_official_data_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )


def _migrate_official_sqlite(settings: Settings, args: argparse.Namespace) -> int:
    parameters = settings.postgres_connection_parameters()
    if not parameters:
        raise ValueError("PostgreSQL settings are required for PIT migration")
    repository = open_official_data_repository(
        postgres_parameters=parameters,
        sqlite_path=settings.market_db_path,
    )
    try:
        imported = repository.import_sqlite(args.source)
        return _emit(
            {
                "source": args.source,
                "target": "postgresql",
                "insertedRows": imported,
                "tradingEnabled": False,
            }
        )
    finally:
        repository.close()


def _sync_symbol_sectors(settings: Settings, args: argparse.Namespace) -> int:
    dart_key = os.environ.get("OPENDART_API_KEY", "")
    if not dart_key:
        raise ValueError("OPENDART_API_KEY is required to sync symbol sectors")
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    from .official_data import OfficialApiClient
    from .sectors import ksic_to_sector
    import urllib.request
    import json

    symbols = args.symbols
    if not symbols:
        postgres_params = settings.postgres_connection_parameters()
        if postgres_params:
            import psycopg

            with psycopg.connect(**postgres_params) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT symbol FROM market_symbols WHERE symbol ~ '^[0-9]{6}$'"
                    )
                    symbols = [r[0] for r in cur.fetchall()]
        else:
            rows = repository._connection.execute(
                "SELECT symbol FROM market_symbols WHERE symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'"
            ).fetchall()
            symbols = [r[0] for r in rows]

    if not symbols:
        return _emit({"synced": 0, "candidates": 0, "tradingEnabled": False})

    client = OfficialApiClient(
        opendart_api_key=dart_key,
        datago_api_key="unused",
    )
    dart_corps = client.dart_corporations()
    clusters: dict[str, str] = {}
    for sym in symbols:
        corp_code = dart_corps.get(sym)
        if not corp_code:
            continue
        try:
            url = f"https://opendart.fss.or.kr/api/company.json?crtfc_key={dart_key}&corp_code={corp_code}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                induty_code = data.get("induty_code")
                sector = ksic_to_sector(induty_code)
                if sector != "UNKNOWN":
                    clusters[sym] = sector
        except Exception:
            continue

    count = repository.upsert_symbol_clusters(clusters)
    repository.close()
    return _emit({"synced": count, "candidates": len(symbols), "tradingEnabled": False})


def _kis_credentials() -> tuple[str, str]:
    key = os.environ.get("KIS_APP_KEY", "")
    secret = os.environ.get("KIS_APP_SECRET", "")
    if not key or not secret:
        raise ValueError("KIS_APP_KEY and KIS_APP_SECRET are required")
    return key, secret


def _serve_paper_mcp(settings: Settings, args: argparse.Namespace) -> int:
    postgres_parameters = settings.postgres_connection_parameters()
    if postgres_parameters is None:
        raise ValueError("serve-paper-mcp requires PostgreSQL settings")
    service = PaperMcpService(
        PostgresPaperReadStore(
            postgres_parameters,
            initial_cash=settings.paper_initial_cash,
        )
    )
    try:
        serve_paper_mcp(host=args.host, port=args.port, service=service)
    except KeyboardInterrupt:
        return 0
    return 0


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
    approved = None if args.status == "all" else args.status == "approved"
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


def _backfill_intraday_samples(
    settings: Settings, args: argparse.Namespace
) -> int:
    max_rank = int(args.max_eligible_rank)
    if not 1 <= max_rank <= 100:
        raise ValueError("max eligible rank must be between 1 and 100")
    seoul = ZoneInfo("Asia/Seoul")
    now = datetime.now(UTC)
    today = now.astimezone(seoul).date()
    session_day = args.as_of or today
    if session_day > today:
        raise ValueError("intraday backfill date must not be in the future")
    probe = datetime.combine(
        session_day, datetime.min.time(), tzinfo=seoul
    ).replace(hour=12)

    postgres_parameters = settings.postgres_connection_parameters()
    with ExitStack() as stack:
        client = _client(settings)
        calendar = MarketCalendarService(client)
        session = calendar.regular_session("KR", now=probe)
        if (
            not session.is_business_day
            or session.market_open_at is None
            or session.market_close_at is None
        ):
            raise ValueError(f"{session_day.isoformat()} is not a KR market day")
        cutoff = (
            min(now, session.market_close_at)
            if session_day == today
            else session.market_close_at
        )
        if cutoff <= session.market_open_at:
            raise ValueError("intraday backfill requires a started market session")

        repository = open_market_repository(
            postgres_parameters=postgres_parameters,
            sqlite_path=settings.market_db_path,
        )
        stack.callback(repository.close)
        universe_store = open_universe_store(
            postgres_parameters=postgres_parameters,
            sqlite_path=settings.paper_db_path,
        )
        stack.callback(universe_store.close)
        if args.symbols:
            symbols = tuple(dict.fromkeys(str(item).upper() for item in args.symbols))
        else:
            start = datetime.combine(
                session_day, datetime.min.time(), tzinfo=seoul
            )
            symbols = universe_store.candidate_symbols_between(
                start.astimezone(UTC),
                cutoff.astimezone(UTC),
                max_eligible_rank=max_rank,
            )
        if not symbols:
            raise ValueError("no observed universe candidates for intraday backfill")

        collector = MarketCollector(client=client, repository=repository)
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        target = session.market_open_at + timedelta(minutes=1)
        for symbol in symbols:
            before_count = _session_candle_count(
                repository, symbol=symbol, session_day=session_day
            )
            cursor = _intraday_backfill_start_cursor(
                session_day=session_day,
                today=today,
                cutoff=cutoff,
            )
            seen_cursors = {cursor} if cursor is not None else set()
            pages = 0
            received = 0
            completion = "page-limit"
            try:
                for _ in range(5):
                    page = collector.collect(
                        symbol=symbol,
                        interval="1m",
                        count=200,
                        before=cursor,
                    )
                    pages += 1
                    received += page.received
                    if (
                        page.oldest_timestamp is not None
                        and page.oldest_timestamp <= target
                    ):
                        completion = "session-open-reached"
                        break
                    if page.next_before is None:
                        completion = "provider-exhausted"
                        break
                    if page.next_before in seen_cursors:
                        raise RuntimeError("intraday cursor made no progress")
                    seen_cursors.add(page.next_before)
                    cursor = page.next_before
                else:
                    raise RuntimeError("intraday pagination limit reached")
                after_count = _session_candle_count(
                    repository, symbol=symbol, session_day=session_day
                )
                results.append(
                    {
                        "symbol": symbol,
                        "pages": pages,
                        "received": received,
                        "storedBefore": before_count,
                        "storedAfter": after_count,
                        "restored": max(0, after_count - before_count),
                        "completion": completion,
                    }
                )
            except (OSError, RuntimeError, TossApiError, TypeError, ValueError) as error:
                failures.append(
                    {"symbol": symbol, "error": f"{type(error).__name__}: {error}"}
                )

    _emit(
        {
            "sessionDate": session_day,
            "cutoff": cutoff,
            "symbolCount": len(symbols),
            "results": results,
            "failures": failures,
            "restoredCandles": sum(int(item["restored"]) for item in results),
            "tradingEnabled": False,
        }
    )
    return 3 if failures else 0


def _intraday_backfill_start_cursor(
    *, session_day: date, today: date, cutoff: datetime
) -> str | None:
    if session_day < today:
        return cutoff.isoformat()
    return None


def _session_candle_count(
    repository: MarketRepository, *, symbol: str, session_day: date
) -> int:
    seoul = ZoneInfo("Asia/Seoul")
    return sum(
        candle.timestamp.astimezone(seoul).date() == session_day
        for candle in repository.latest_candles(symbol, "1m", limit=10_000)
    )


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


def _backtest_ma(settings: Settings, args: argparse.Namespace) -> int:
    if args.count <= 0:
        raise ValueError("count must be positive")
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    try:
        candles = repository.latest_candles(
            args.symbol.upper(), args.interval, limit=args.count
        )
    finally:
        repository.close()
    result = run_ma_backtest(
        candles=candles,
        quantity=args.quantity,
        initial_cash=args.initial_cash or settings.paper_initial_cash,
        short_window=args.short_window,
        long_window=args.long_window,
        slippage_rate=args.slippage_bps / Decimal(10000),
    )
    return _emit(asdict(result))


def _backtest_portfolio_ma(settings: Settings, args: argparse.Namespace) -> int:
    if args.count <= 0:
        raise ValueError("count must be positive")
    symbols = tuple(sorted(symbol.upper() for symbol in args.symbols))
    if len(set(symbols)) != len(symbols):
        raise ValueError("symbols must not contain duplicates")
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    try:
        candles_by_symbol = {
            symbol: repository.latest_candles(symbol, args.interval, limit=args.count)
            for symbol in symbols
        }
    finally:
        repository.close()
    result = run_ma_portfolio_backtest(
        candles_by_symbol=candles_by_symbol,
        quantity=args.quantity,
        initial_cash=(
            args.initial_cash
            if args.initial_cash is not None
            else settings.paper_initial_cash
        ),
        short_window=args.short_window,
        long_window=args.long_window,
        slippage_rate=args.slippage_bps / Decimal(10000),
        max_open_positions=args.max_open_positions,
        max_daily_buys=args.max_daily_buys,
        max_position_notional=args.max_position_notional,
        max_order_notional=args.max_order_notional,
    )
    if args.output_format == "csv":
        return _emit_portfolio_backtest_csv(result)
    return _emit(asdict(result))


def _emit_portfolio_backtest_csv(result: PortfolioBacktestResult) -> int:
    fieldnames = (
        "symbol",
        "interval",
        "currency",
        "portfolio_initial_cash",
        "portfolio_final_cash",
        "portfolio_position_market_value",
        "portfolio_final_equity",
        "portfolio_total_return_rate",
        "portfolio_buy_hold_return_rate",
        "portfolio_excess_return_rate",
        "portfolio_max_drawdown_rate",
        "portfolio_realized_pnl",
        "portfolio_unrealized_pnl",
        "portfolio_total_costs",
        "portfolio_insufficient_cash_buys",
        "portfolio_max_open_position_rejections",
        "portfolio_max_daily_buy_rejections",
        "portfolio_max_position_notional_rejections",
        "portfolio_max_order_notional_rejections",
        "symbol_candle_count",
        "symbol_quantity",
        "symbol_cost_basis",
        "symbol_average_cost",
        "symbol_market_price",
        "symbol_market_value",
        "symbol_realized_pnl",
        "symbol_unrealized_pnl",
        "symbol_total_costs",
        "symbol_trade_count",
        "symbol_completed_trades",
        "symbol_winning_trades",
        "symbol_insufficient_cash_buys",
        "symbol_max_open_position_rejections",
        "symbol_max_daily_buy_rejections",
        "symbol_max_position_notional_rejections",
        "symbol_max_order_notional_rejections",
    )
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for position in result.positions:
        writer.writerow(
            {
                "symbol": position.symbol,
                "interval": result.interval,
                "currency": result.currency,
                "portfolio_initial_cash": result.initial_cash,
                "portfolio_final_cash": result.final_cash,
                "portfolio_position_market_value": result.position_market_value,
                "portfolio_final_equity": result.final_equity,
                "portfolio_total_return_rate": result.total_return_rate,
                "portfolio_buy_hold_return_rate": result.buy_hold_return_rate,
                "portfolio_excess_return_rate": result.excess_return_rate,
                "portfolio_max_drawdown_rate": result.max_drawdown_rate,
                "portfolio_realized_pnl": result.realized_pnl,
                "portfolio_unrealized_pnl": result.unrealized_pnl,
                "portfolio_total_costs": result.total_costs,
                "portfolio_insufficient_cash_buys": (result.insufficient_cash_buys),
                "portfolio_max_open_position_rejections": (
                    result.max_open_position_rejections
                ),
                "portfolio_max_daily_buy_rejections": (result.max_daily_buy_rejections),
                "portfolio_max_position_notional_rejections": (
                    result.max_position_notional_rejections
                ),
                "portfolio_max_order_notional_rejections": (
                    result.max_order_notional_rejections
                ),
                "symbol_candle_count": position.candle_count,
                "symbol_quantity": position.quantity,
                "symbol_cost_basis": position.cost_basis,
                "symbol_average_cost": position.average_cost,
                "symbol_market_price": position.market_price,
                "symbol_market_value": position.market_value,
                "symbol_realized_pnl": position.realized_pnl,
                "symbol_unrealized_pnl": position.unrealized_pnl,
                "symbol_total_costs": position.total_costs,
                "symbol_trade_count": position.trade_count,
                "symbol_completed_trades": position.completed_trades,
                "symbol_winning_trades": position.winning_trades,
                "symbol_insufficient_cash_buys": (position.insufficient_cash_buys),
                "symbol_max_open_position_rejections": (
                    position.max_open_position_rejections
                ),
                "symbol_max_daily_buy_rejections": (position.max_daily_buy_rejections),
                "symbol_max_position_notional_rejections": (
                    position.max_position_notional_rejections
                ),
                "symbol_max_order_notional_rejections": (
                    position.max_order_notional_rejections
                ),
            }
        )
    return 0


def _serve_paper_timeline(settings: Settings, args: argparse.Namespace) -> int:
    parameters = settings.postgres_connection_parameters()
    if parameters is None:
        raise ValueError("paper timeline requires PostgreSQL configuration")
    store = PostgresPaperTimelineStore(
        parameters,
        initial_cash=settings.paper_initial_cash,
    )
    payload = store.payload()
    print(
        json.dumps(
            {
                "timelineServer": "listening",
                "host": args.host,
                "port": args.port,
                "portfolios": ["rule", "hermes"],
                "days": len(payload["meta"]["dates"]),
                "readOnly": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    serve_timeline(host=args.host, port=args.port, payload=store.payload)
    return 0


def _walk_forward_ma(settings: Settings, args: argparse.Namespace) -> int:
    if args.count <= 0:
        raise ValueError("count must be positive")
    repository = open_market_repository(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.market_db_path,
    )
    try:
        candles = repository.latest_candles(
            args.symbol.upper(), args.interval, limit=args.count
        )
    finally:
        repository.close()
    result = run_ma_walk_forward(
        candles=candles,
        short_windows=args.short_windows,
        long_windows=args.long_windows,
        train_ratio=args.train_ratio,
        quantity=args.quantity,
        initial_cash=args.initial_cash or settings.paper_initial_cash,
        slippage_rate=args.slippage_bps / Decimal(10000),
    )
    if args.output_format == "csv":
        return _emit_walk_forward_csv(result)
    return _emit(asdict(result))


def _emit_walk_forward_csv(result: WalkForwardResult) -> int:
    fieldnames = (
        "symbol",
        "interval",
        "selected",
        "short_window",
        "long_window",
        "train_rank",
        "validation_rank",
        "overfit_warning",
        "train_return_rate",
        "train_excess_return_rate",
        "train_max_drawdown_rate",
        "train_completed_trades",
        "train_win_rate",
        "train_total_costs",
        "validation_return_rate",
        "validation_excess_return_rate",
        "validation_max_drawdown_rate",
        "validation_completed_trades",
        "validation_win_rate",
        "validation_total_costs",
    )
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for candidate in result.candidates:
        writer.writerow(
            {
                "symbol": result.symbol,
                "interval": result.interval,
                "selected": candidate.train_rank == 1,
                "short_window": candidate.short_window,
                "long_window": candidate.long_window,
                "train_rank": candidate.train_rank,
                "validation_rank": candidate.validation_rank,
                "overfit_warning": candidate.overfit_warning,
                "train_return_rate": candidate.train.total_return_rate,
                "train_excess_return_rate": candidate.train.excess_return_rate,
                "train_max_drawdown_rate": candidate.train.max_drawdown_rate,
                "train_completed_trades": candidate.train.completed_trades,
                "train_win_rate": candidate.train.win_rate,
                "train_total_costs": candidate.train.total_costs,
                "validation_return_rate": candidate.validation.total_return_rate,
                "validation_excess_return_rate": (
                    candidate.validation.excess_return_rate
                ),
                "validation_max_drawdown_rate": (
                    candidate.validation.max_drawdown_rate
                ),
                "validation_completed_trades": (candidate.validation.completed_trades),
                "validation_win_rate": candidate.validation.win_rate,
                "validation_total_costs": candidate.validation.total_costs,
            }
        )
    return 0


def _run_paper_cycle(settings: Settings, args: argparse.Namespace) -> int:
    if args.hermes_advisor and args.portfolio != "hermes":
        raise ValueError("Hermes advisor is only valid for the hermes portfolio")
    if args.snapshot_stdin and args.portfolio != "hermes":
        raise ValueError("shared snapshot input is only valid for the hermes portfolio")
    snapshot = _read_cycle_snapshot() if args.snapshot_stdin else None
    hunter_candidates = _approved_hunter_candidates(
        snapshot.hunter_entry if snapshot is not None else None
    )
    explicit_symbols = (
        snapshot.symbols
        if snapshot is not None
        else (
            tuple(symbol.upper() for symbol in args.symbols) if args.symbols else None
        )
    )
    interval = (
        snapshot.interval
        if snapshot is not None
        else (args.interval or settings.strategy_interval)
    )
    short_window = args.short_window or settings.strategy_short_window
    long_window = args.long_window or settings.strategy_long_window
    quantity = args.quantity or settings.paper_order_quantity
    if not 0 < short_window < long_window <= 199:
        raise ValueError("windows must satisfy 0 < short_window < long_window <= 199")
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    postgres_parameters = settings.postgres_connection_parameters()
    now = snapshot.evaluated_at if snapshot is not None else datetime.now(UTC)
    market_context = None
    momentum_shadow = None
    momentum_ranked_symbols: tuple[str, ...] = ()
    momentum_ranking_error: str | None = None
    momentum_research_pool: tuple[str, ...] = ()
    momentum_markets: dict[str, str] = {}
    momentum_context_ready = False
    momentum_evaluation_due = False
    momentum_session: MarketSession | None = None
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
            initial_cash=settings.paper_initial_cash,
        )
        if snapshot is not None and args.portfolio == "hermes":
            snapshot = _extend_cycle_snapshot(
                snapshot,
                (
                    *performance.open_position_symbols(),
                    *hunter_candidates,
                ),
            )
            symbols = snapshot.symbols
            explicit_symbols = snapshot.symbols
        collector = MarketCollector(client=client, repository=market_repository)
        v2_strategy = OfficialV2CycleStrategy(
            market_repository,
            context_factory=OfficialSetupContextFactory(
                settings.market_db_path,
                postgres_parameters=settings.postgres_connection_parameters(),
            ),
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
            calendar = MarketCalendarService(client)
            official_repository = _official_repository(settings)
            stack.callback(official_repository.close)

            def opening_rankings(now: datetime) -> dict:
                if not settings.krx_api_key:
                    raise RuntimeError(
                        "KRX_API_KEY is required for D-1 opening rankings"
                    )
                session = previous_kr_business_date(calendar, now)
                known_symbols = frozenset(official_repository.symbols())
                if not known_symbols:
                    raise RuntimeError("known opening pool is empty")
                payload = fetch_krx_acc_trdval_rankings(
                    api_key=settings.krx_api_key,
                    bas_dd=session.strftime("%Y%m%d"),
                    count=len(known_symbols),
                    ranked_at=now,
                    allowed_symbols=known_symbols,
                )
                payload["rankingSession"] = session.isoformat()
                return payload

            def candidate_gate(symbol: str, now: datetime) -> RiskDecision:
                decision = v2_strategy.build_candidate(symbol, now=now).decision
                reasons = (
                    *(f"missing:{value}" for value in decision.missing_checks),
                    *(f"violation:{value}" for value in decision.violations),
                )
                return RiskDecision(decision.approved, reasons)

            def krx_amount_rankings(now: datetime, count: int) -> dict:
                if not settings.krx_api_key:
                    raise RuntimeError(
                        "KRX_API_KEY is required for afternoon universe rankings"
                    )
                session = previous_kr_business_date(calendar, now)
                return fetch_krx_acc_trdval_rankings(
                    api_key=settings.krx_api_key,
                    bas_dd=session.strftime("%Y%m%d"),
                    count=count,
                    ranked_at=now,
                )

            universe_result = DynamicUniverseSelector(
                client=client,
                collector=collector,
                repository=market_repository,
                store=universe_store,
                risk_manager=RiskManager(RiskLimits()),
                candidate_count=settings.dynamic_universe_candidate_count,
                ranking_fetch_count=settings.dynamic_universe_ranking_fetch_count,
                universe_size=settings.dynamic_universe_size,
                krx_amount_rankings=krx_amount_rankings,
                opening_rankings=opening_rankings,
                candidate_gate=candidate_gate,
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
            local_time = now.astimezone(ZoneInfo("Asia/Seoul")).time()
            if (
                args.portfolio == "rule"
                and interval == "1m"
                and time(9, 0) <= local_time < time(10, 6)
            ):
                try:
                    signal_session = previous_kr_business_date(calendar, now)
                    day_start = datetime.combine(
                        now.astimezone(ZoneInfo("Asia/Seoul")).date(),
                        datetime.min.time(),
                        tzinfo=ZoneInfo("Asia/Seoul"),
                    ).astimezone(UTC)
                    momentum_research_pool = (
                        universe_store.latest_candidates_between(
                            day_start,
                            now,
                            ranking_source=RANKING_SOURCE_OPENING,
                        )
                        or ()
                    )
                    momentum_markets = official_repository.market_categories(
                        signal_session
                    )
                    momentum_session = calendar.regular_session("KR", now=now)
                    momentum_context_ready = True
                    if time(10, 0) <= local_time < time(10, 6):
                        momentum_evaluation_due = not _momentum_shadow_recorded(
                            paper_ledger,
                            session_date=momentum_session.business_date.isoformat(),
                        )
                    if local_time <= time(10, 0):
                        momentum_ranked_symbols = _momentum_ranked_symbols(
                            client,
                            allowed_symbols=frozenset(momentum_research_pool),
                        )
                    elif momentum_evaluation_due:
                        momentum_ranked_symbols = _momentum_observed_symbols(
                            market_repository,
                            symbols=momentum_research_pool,
                            session=momentum_session,
                            observed_at=now,
                        )
                    else:
                        momentum_ranked_symbols = _recorded_momentum_symbols(
                            paper_ledger,
                            session_date=momentum_session.business_date.isoformat(),
                        )
                except (
                    OSError,
                    RuntimeError,
                    TossApiError,
                    TypeError,
                    ValueError,
                ) as error:
                    momentum_ranking_error = f"{type(error).__name__}: {error}"
        intraday_sample = None
        if interval == "1m" and universe_result is not None:
            intraday_sample = _collect_intraday_sample(
                collector,
                cycle_symbols=symbols,
                collection_symbols=tuple(
                    dict.fromkeys(
                        (*universe_result.collection_symbols, *momentum_ranked_symbols)
                    )
                ),
                extra_symbols=_momentum_collection_symbols(
                    research_pool=momentum_research_pool,
                    benchmark_symbols=settings.market_benchmark_symbols,
                    evaluation_due=momentum_evaluation_due,
                ),
                extra_count=SESSION_MINUTE_FETCH_COUNT,
            )
            intraday_sample["momentumRankingSymbols"] = list(
                momentum_ranked_symbols
            )
            intraday_sample["momentumRankingError"] = momentum_ranking_error
        if (
            args.portfolio == "rule"
            and interval == "1m"
            and universe_result is not None
            and momentum_context_ready
            and time(10, 0)
            <= now.astimezone(ZoneInfo("Asia/Seoul")).time()
            < time(10, 6)
        ):
            try:
                assert momentum_session is not None
                momentum_shadow = evaluate_momentum_shadow(
                    market_repository,
                    symbols=momentum_research_pool,
                    market_by_symbol=momentum_markets,
                    session=momentum_session,
                    observed_at=now,
                )
            except (OSError, RuntimeError, TossApiError, TypeError, ValueError) as error:
                momentum_shadow = {
                    "status": "unavailable",
                    "ruleVersion": MOMENTUM_SHADOW_RULE_VERSION,
                    "error": f"{type(error).__name__}: {error}",
                }
        elif (
            args.portfolio == "rule"
            and interval == "1m"
            and universe_result is not None
            and time(10, 0)
            <= now.astimezone(ZoneInfo("Asia/Seoul")).time()
            < time(10, 6)
        ):
            momentum_shadow = {
                "status": "unavailable",
                "ruleVersion": MOMENTUM_SHADOW_RULE_VERSION,
                "error": momentum_ranking_error or "momentum context unavailable",
            }
        name_symbols = tuple(
            dict.fromkeys(
                (
                    *symbols,
                    *(
                        universe_result.collection_symbols
                        if universe_result is not None
                        else ()
                    ),
                    *momentum_ranked_symbols,
                    *settings.market_benchmark_symbols,
                )
            )
        )
        symbol_names = (
            collector.resolve_symbol_names(name_symbols) if name_symbols else {}
        )
        if (
            args.portfolio == "rule"
            and isinstance(momentum_shadow, dict)
            and momentum_shadow.get("status") == "evaluated"
        ):
            try:
                momentum_shadow["hermes"] = review_momentum_shadow_once(
                    api_key=os.environ.get("HERMES_API_KEY", ""),
                    base_url=os.environ.get(
                        "HERMES_API_BASE_URL", "http://hermes-analysis:8642"
                    ),
                    audit=paper_ledger,
                    payload=momentum_shadow,
                    symbol_names=symbol_names,
                )
            except Exception as error:  # noqa: BLE001
                momentum_shadow["hermes"] = {
                    "status": "unavailable",
                    "error": f"{type(error).__name__}: {error}",
                }
            _record_momentum_shadow_once(
                paper_ledger,
                payload=momentum_shadow,
                observed_at=now,
            )
        result = PaperCycleRunner(
            collector=collector,
            strategy=StoredMaStrategy(market_repository),
            trading=PaperTradingService(
                ledger=paper_ledger,
                risk_manager=_cycle_risk_manager(),
                initial_cash=settings.paper_initial_cash,
                advisor=(
                    create_hermes_trade_advisor(
                        api_key=os.environ.get("HERMES_API_KEY", ""),
                        base_url=os.environ.get(
                            "HERMES_API_BASE_URL", "http://hermes-analysis:8642"
                        ),
                        audit=paper_ledger,
                        symbol_names=symbol_names,
                    )
                    if args.hermes_advisor
                    else None
                ),
            ),
            calendar=MarketCalendarService(client),
            performance=performance,
            state=cycle_state,
            v2_strategy=v2_strategy,
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
            experimental_strategy_reference=(
                args.portfolio == "hermes" and args.hermes_advisor
            ),
            hunter_candidates=hunter_candidates,
            snapshot=snapshot,
            daily_snapshot_prepared=universe_result is not None,
        )
        cash_balance = paper_ledger.cash_balance(settings.paper_initial_cash)
        intraday_review = (
            _intraday_review_for_day(cycle_state, now)
            if interval == "1d"
            else None
        )
        session_accounting = (
            _paper_session_accounting(
                paper_ledger,
                now=now,
                current_cycle_fills=result.fill_count,
                initial_cash=settings.paper_initial_cash,
                cash_balance=cash_balance,
                equity=result.equity,
                symbol_names=symbol_names,
            )
            if interval == "1d"
            else None
        )
        hermes_snapshot = None
        if interval == "1m" and universe_result is not None:
            failed_samples = {
                str(item["symbol"]): str(item["error"])
                for item in (intraday_sample or {}).get("failures", [])
            }
            hermes_snapshot = _cycle_snapshot_to_dict(
                replace(
                    _hermes_candidate_snapshot(
                        result.snapshot,
                        universe_result.collection_symbols,
                        failed_samples=failed_samples,
                    ),
                    hunter_entry=(
                        _hunter_entry_payload(momentum_shadow)
                        if isinstance(momentum_shadow, Mapping)
                        else None
                    ),
                ),
                symbol_names=symbol_names,
            )
        if interval == "1d":
            try:
                session = MarketCalendarService(client).regular_session(
                    "KR", now=now
                )
                watched = tuple(
                    dict.fromkeys(
                        (
                            *(
                                universe_result.collection_symbols
                                if universe_result is not None
                                else ()
                            ),
                            *symbols,
                        )
                    )
                )
                fill_symbols = tuple(
                    dict.fromkeys(
                        (*settings.market_benchmark_symbols, *watched)
                    )
                )
                _collect_session_minutes(
                    collector,
                    fill_symbols,
                    count=SESSION_MINUTE_FETCH_COUNT,
                )
                market_context = build_market_context(
                    market_repository,
                    symbols=watched,
                    benchmark_symbols=settings.market_benchmark_symbols,
                    session=session,
                    now=now,
                    names=symbol_names,
                    entry_arm_window=V2_ENTRY_ARM_WINDOW,
                    first_bar_offset=COMPLETED_ONE_MINUTE_OFFSET,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                market_context = {
                    "status": "unavailable",
                    "error": type(error).__name__,
                }
            if intraday_sample is None:
                intraday_sample = {
                    "applicable": False,
                    "reason": "1d-cycle-reads-stored-1m-via-marketContext",
                }
    _emit(
        {
            "portfolioId": args.portfolio,
            "runId": result.run_id,
            "startedAt": result.started_at,
            "finishedAt": result.finished_at,
            "interval": result.interval,
            "entryStrategy": (
                "hermes-experimental-v2.3-reference-gates"
                if args.portfolio == "hermes" and args.hermes_advisor
                else "setup-v2.3-independent-daily"
            ),
            "dailyReturnRate": result.daily_return_rate,
            "currencyReturns": result.currency_returns,
            "equity": result.equity,
            "realizedPnl": result.realized_pnl,
            "unrealizedPnl": result.unrealized_pnl,
            "totalCosts": result.total_costs,
            "initialCash": settings.paper_initial_cash,
            "cashBalance": cash_balance,
            "consecutiveApiErrors": result.consecutive_api_errors,
            "sharedSnapshot": _cycle_snapshot_to_dict(
                result.snapshot, symbol_names=symbol_names
            ),
            "hermesSnapshot": hermes_snapshot,
            "instruments": [
                {"symbol": symbol, "name": symbol_names[symbol]} for symbol in symbols
            ],
            "universe": (
                {
                    "runId": universe_result.run_id,
                    "refreshed": universe_result.refreshed,
                    "cacheHit": universe_result.run_id is None
                    and not universe_result.refreshed,
                    "cacheMeaning": (
                        "same-day freeze. runId is set only when the selector refreshes."
                    ),
                    "symbols": list(universe_result.symbols),
                    "newBuysAllowed": universe_result.new_buys_allowed,
                    "entrySymbols": list(universe_result.entry_symbols),
                    "collectionSymbols": list(universe_result.collection_symbols),
                }
                if universe_result is not None
                else {
                    "source": (
                        "shared-hermes-candidate-pool"
                        if snapshot is not None and args.portfolio == "hermes"
                        else "explicit"
                    ),
                    "symbols": list(symbols),
                }
            ),
            "evaluationPool": {
                "role": (
                    "hermes-experimental-static-top30"
                    if snapshot is not None and args.portfolio == "hermes"
                    else "rule-top15"
                ),
                "symbolCount": len(symbols),
                "directlyComparable": not (
                    snapshot is not None and args.portfolio == "hermes"
                ),
            },
            "intradaySample": intraday_sample,
            "momentumShadow": momentum_shadow,
            "summary": {
                "symbols": result.symbol_count,
                "signals": result.signal_count,
                "fills": result.fill_count,
                "fillScope": "current-cycle",
                "skipped": result.skipped_count,
                "failed": result.failed_count,
                "idleReason": result.insight["idleReason"],
            },
            **(
                {"sessionAccountingV1": session_accounting}
                if session_accounting is not None
                else {}
            ),
            "intradayReview": intraday_review,
            "marketContext": market_context,
            "items": [
                {**asdict(item), "name": symbol_names[item.symbol]}
                for item in result.items
            ],
        }
    )
    return 3 if result.failed_count else 0


def _seoul_day_window(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC), now.astimezone(UTC)


def _paper_session_accounting(
    ledger: PaperLedgerStore,
    *,
    now: datetime,
    current_cycle_fills: int,
    initial_cash: Decimal,
    cash_balance: Decimal,
    equity: Decimal,
    symbol_names: Mapping[str, str],
) -> dict[str, Any]:
    started_at, observed_at = _seoul_day_window(now)
    fills = (
        ledger.fills_between(started_at, observed_at)
        if started_at <= observed_at
        else ()
    )
    accountings = ledger.position_accountings()
    local_zone = ZoneInfo("Asia/Seoul")
    baseline = ledger.daily_equity_baseline(now)
    return {
        "schemaVersion": 1,
        "scope": "seoul-market-session-to-observed-at",
        "sessionStart": started_at.astimezone(local_zone).isoformat(),
        "observedAt": observed_at.astimezone(local_zone).isoformat(),
        "currentCycleFillScope": "current-cycle",
        "currentCycleFills": current_cycle_fills,
        "sessionFillScope": "seoul-session-cumulative",
        "sessionBuyFills": sum(fill.side is Side.BUY for fill in fills),
        "sessionSellFills": sum(fill.side is Side.SELL for fill in fills),
        "sessionFills": [
            {
                "symbol": fill.symbol,
                "name": symbol_names.get(fill.symbol, fill.symbol),
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "notional": str(fill.notional),
                "commission": str(fill.commission),
                "tax": str(fill.tax),
                "executedAt": fill.executed_at.astimezone(local_zone).isoformat(),
            }
            for fill in fills
        ],
        "dailyBaselineEquity": str(baseline) if baseline is not None else None,
        "initialCash": str(initial_cash),
        "cashBalance": str(cash_balance),
        "equity": str(equity),
        "positions": [
            {
                "symbol": accounting.symbol,
                "name": symbol_names.get(accounting.symbol, accounting.symbol),
                "quantity": str(accounting.quantity),
                "costBasis": str(accounting.cost_basis),
                "realizedPnl": str(accounting.realized_pnl),
                "totalCosts": str(accounting.total_costs),
            }
            for accounting in sorted(
                accountings.values(), key=lambda value: value.symbol
            )
            if accounting.quantity > 0
        ],
    }


def _intraday_review_for_day(store: CycleStateStore, now: datetime) -> dict[str, Any]:
    started_from, started_to = _seoul_day_window(now)
    runs = store.list_runs(
        interval="1m", started_from=started_from, started_to=started_to
    )
    return aggregate_intraday_review(
        insights_from_runs(runs), cycle_count=len(runs)
    )


def _hermes_candidate_snapshot(
    snapshot: PaperCycleSnapshot,
    collection_symbols: tuple[str, ...],
    *,
    failed_samples: Mapping[str, str],
) -> PaperCycleSnapshot:
    symbols = tuple(dict.fromkeys(collection_symbols)) or snapshot.symbols
    source_errors = dict(zip(snapshot.symbols, snapshot.errors, strict=True))
    errors = tuple(
        failed_samples.get(symbol) or source_errors.get(symbol) for symbol in symbols
    )
    return PaperCycleSnapshot(
        evaluated_at=snapshot.evaluated_at,
        symbols=symbols,
        interval=snapshot.interval,
        collections=(None,) * len(symbols),
        signals=(None,) * len(symbols),
        skips=(None,) * len(symbols),
        errors=errors,
        api_failed=snapshot.api_failed,
        new_buys_allowed=snapshot.new_buys_allowed,
        ma_states=(None,) * len(symbols),
        hunter_entry=snapshot.hunter_entry,
    )


def _extend_cycle_snapshot(
    snapshot: PaperCycleSnapshot, extra_symbols: tuple[str, ...]
) -> PaperCycleSnapshot:
    missing = tuple(
        symbol for symbol in dict.fromkeys(extra_symbols) if symbol not in snapshot.symbols
    )
    if not missing:
        return snapshot
    return replace(
        snapshot,
        symbols=(*snapshot.symbols, *missing),
        collections=(*snapshot.collections, *((None,) * len(missing))),
        signals=(*snapshot.signals, *((None,) * len(missing))),
        skips=(*snapshot.skips, *((None,) * len(missing))),
        errors=(*snapshot.errors, *((None,) * len(missing))),
        ma_states=(None,) * (len(snapshot.symbols) + len(missing)),
        v2_candidates=(),
    )


def _cycle_snapshot_to_dict(
    snapshot: PaperCycleSnapshot,
    *,
    symbol_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    names = symbol_names or {}
    return {
        "version": 1,
        "evaluatedAt": snapshot.evaluated_at,
        "symbols": list(snapshot.symbols),
        "instruments": [
            {"symbol": symbol, "name": names.get(symbol, symbol)}
            for symbol in snapshot.symbols
        ],
        "interval": snapshot.interval,
        "collections": [
            (
                {
                    "symbol": item.symbol,
                    "interval": item.interval,
                    "received": item.received,
                    "upserted": item.upserted,
                    "nextBefore": item.next_before,
                }
                if item is not None
                else None
            )
            for item in snapshot.collections
        ],
        "signals": [
            (
                {
                    "signalId": item.signal_id,
                    "symbol": item.symbol,
                    "side": item.side.value,
                    "referencePrice": item.reference_price,
                    "quantity": item.quantity,
                    "reason": item.reason,
                }
                if item is not None
                else None
            )
            for item in snapshot.signals
        ],
        "skips": list(snapshot.skips),
        "errors": list(snapshot.errors),
        "apiFailed": snapshot.api_failed,
        "newBuysAllowed": snapshot.new_buys_allowed,
        "maStates": [
            (
                {
                    "close": str(item.close),
                    "maShort": str(item.short_ma),
                    "maLong": str(item.long_ma),
                    "relation": item.relation,
                }
                if item is not None
                else None
            )
            for item in snapshot.ma_states
        ],
        "hunterEntry": (
            dict(snapshot.hunter_entry)
            if isinstance(snapshot.hunter_entry, Mapping)
            else None
        ),
    }


def _cycle_risk_manager() -> RiskManager | N8nRiskManager:
    webhook_url = os.environ.get("RISK_MANAGER_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return RiskManager(RiskLimits())
    return N8nRiskManager(
        webhook_url=webhook_url,
        token=os.environ.get("N8N_RISK_MANAGER_TOKEN", ""),
    )


def _read_cycle_snapshot() -> PaperCycleSnapshot:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("shared snapshot stdin must contain JSON") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("shared snapshot version is unsupported")
    symbols = payload.get("symbols")
    collections = payload.get("collections")
    signals = payload.get("signals")
    default_skips = [None] * len(symbols) if isinstance(symbols, list) else []
    skips = payload.get("skips", default_skips)
    errors = payload.get("errors")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("shared snapshot symbols must be a non-empty list")
    if not all(isinstance(symbol, str) and symbol for symbol in symbols):
        raise ValueError("shared snapshot contains an invalid symbol")
    if not all(
        isinstance(values, list) for values in (collections, signals, skips, errors)
    ):
        raise ValueError("shared snapshot arrays are missing")
    evaluated_at = datetime.fromisoformat(str(payload.get("evaluatedAt", "")))
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("shared snapshot evaluatedAt must include timezone")
    interval = payload.get("interval")
    if interval not in {"1m", "1d"}:
        raise ValueError("shared snapshot interval is invalid")
    parsed_collections = tuple(
        (
            CollectionResult(
                symbol=str(item["symbol"]),
                interval=str(item["interval"]),
                received=int(item["received"]),
                upserted=int(item["upserted"]),
                next_before=(
                    str(item["nextBefore"])
                    if item.get("nextBefore") is not None
                    else None
                ),
            )
            if isinstance(item, dict)
            else None
        )
        for item in collections
    )
    parsed_signals = tuple(
        (
            TradeSignal(
                signal_id=str(item["signalId"]),
                symbol=str(item["symbol"]),
                side=Side(str(item["side"])),
                reference_price=Decimal(str(item["referencePrice"])),
                quantity=Decimal(str(item["quantity"])),
                reason=str(item["reason"]),
            )
            if isinstance(item, dict)
            else None
        )
        for item in signals
    )
    parsed_errors = tuple(None if error is None else str(error) for error in errors)
    parsed_skips = tuple(None if reason is None else str(reason) for reason in skips)
    api_failed = payload.get("apiFailed")
    if not isinstance(api_failed, bool):
        raise TypeError("shared snapshot apiFailed must be boolean")
    new_buys_allowed = payload.get("newBuysAllowed")
    if not isinstance(new_buys_allowed, bool):
        raise TypeError("shared snapshot newBuysAllowed must be boolean")
    raw_ma_states = payload.get("maStates")
    raw_hunter_entry = payload.get("hunterEntry")
    if raw_hunter_entry is not None and not isinstance(
        raw_hunter_entry, Mapping
    ):
        raise TypeError("shared snapshot hunterEntry must be an object")
    if raw_ma_states is None:
        parsed_ma_states: tuple[MaCrossoverEvaluation | None, ...] = ()
    elif not isinstance(raw_ma_states, list) or len(raw_ma_states) != len(symbols):
        raise ValueError("shared snapshot maStates do not match symbols")
    else:
        parsed_ma_states = tuple(
            (
                MaCrossoverEvaluation(
                    signal=parsed_signals[index],
                    close=Decimal(str(item["close"])),
                    short_ma=Decimal(str(item["maShort"])),
                    long_ma=Decimal(str(item["maLong"])),
                    relation=str(item["relation"]),
                )
                if isinstance(item, dict)
                else None
            )
            for index, item in enumerate(raw_ma_states)
        )
    return PaperCycleSnapshot(
        evaluated_at=evaluated_at,
        symbols=tuple(symbol.upper() for symbol in symbols),
        interval=interval,
        collections=parsed_collections,
        signals=parsed_signals,
        skips=parsed_skips,
        errors=parsed_errors,
        api_failed=api_failed,
        new_buys_allowed=new_buys_allowed,
        ma_states=parsed_ma_states,
        hunter_entry=(
            dict(raw_hunter_entry)
            if isinstance(raw_hunter_entry, Mapping)
            else None
        ),
    )


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
        collector = MarketCollector(client=_client(settings), repository=repository)
        result = V2MarketScanner(
            collector=collector,
            repository=repository,
            candidate_builder=OfficialV2CycleStrategy(
                repository,
                context_factory=OfficialSetupContextFactory(
                    settings.market_db_path,
                    postgres_parameters=settings.postgres_connection_parameters(),
                ),
            ),
        ).run(
            benchmark_symbols=benchmarks,
            discovery_symbols=symbols,
            top_n=top_n,
            now=datetime.now(UTC),
        )
    finally:
        repository.close()
    _emit(v2_market_scan_to_dict(result))
    return 3 if result.errors else 0


def _optional_kr_session_lookup(settings: Settings):
    if not settings.client_id or not settings.client_secret:
        return None
    return _market_session_lookup_from_env()


def _render_metrics(settings: Settings) -> int:
    store = open_metrics_store(
        postgres_parameters=settings.postgres_connection_parameters(),
        sqlite_path=settings.paper_db_path,
    )
    try:
        sys.stdout.write(
            MetricsService(
                store,
                initial_cash=settings.paper_initial_cash,
                session_lookup=_optional_kr_session_lookup(settings),
            ).render()
        )
    finally:
        store.close()
    return 0


def _momentum_ranked_symbols(
    client: TossClient,
    *,
    allowed_symbols: frozenset[str],
) -> tuple[str, ...]:
    payload = client.rankings(
        ranking_type="TOP_GAINERS",
        market_country="KR",
        duration="realtime",
        exclude_investment_caution=True,
        count=100,
    )
    return momentum_ranking_symbols(
        payload,
        allowed_symbols=allowed_symbols,
        limit=MOMENTUM_SAMPLE_LIMIT,
    )


def _approved_hunter_candidates(
    payload: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(payload, Mapping)
        or payload.get("status") != "evaluated"
        or payload.get("ruleVersion") != MOMENTUM_SHADOW_RULE_VERSION
        or payload.get("strategyInput") is not True
        or payload.get("shadowOnly") is not False
        or payload.get("paperOnly") is not True
    ):
        return {}
    selected = payload.get("selected")
    hermes = payload.get("hermes")
    decisions = hermes.get("decisions") if isinstance(hermes, Mapping) else None
    if (
        not isinstance(selected, Sequence)
        or isinstance(selected, (str, bytes))
        or not isinstance(decisions, Sequence)
        or isinstance(decisions, (str, bytes))
    ):
        return {}
    approved = {
        str(row.get("symbol") or "")
        for row in decisions
        if isinstance(row, Mapping) and row.get("verdict") == "approve"
    }
    session_date = str(payload.get("sessionDate") or "")
    result: dict[str, dict[str, Any]] = {}
    for row in selected:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "")
        if symbol not in approved:
            continue
        result[symbol] = {**dict(row), "sessionDate": session_date}
    return result


def _hunter_entry_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(payload),
        "strategyInput": True,
        "shadowOnly": False,
        "paperOnly": True,
        "promotedFrom": MOMENTUM_SHADOW_RULE_VERSION,
    }


def _momentum_observed_symbols(
    repository: MarketRepository,
    *,
    symbols: Sequence[str],
    session: MarketSession,
    observed_at: datetime,
) -> tuple[str, ...]:
    if session.market_open_at is None:
        return ()
    cutoff = min(
        observed_at,
        session.market_open_at.replace(hour=10, minute=0),
    )
    observed = []
    for symbol in dict.fromkeys(symbols):
        rows = repository.latest_candles(symbol, "1m", limit=60)
        if any(
            session.market_open_at < candle.timestamp <= cutoff for candle in rows
        ):
            observed.append(symbol)
    return tuple(observed)


def _record_momentum_shadow_once(
    ledger: Any,
    *,
    payload: dict[str, Any],
    observed_at: datetime,
) -> str:
    session_date = str(payload.get("sessionDate") or "")
    if not session_date:
        raise ValueError("momentum shadow sessionDate is required")
    for run in ledger.recent_automation_runs(
        limit=100, run_type="momentum-shadow"
    ):
        details = run.get("details")
        if not isinstance(details, Mapping):
            continue
        if (
            details.get("sessionDate") == session_date
            and details.get("ruleVersion") == MOMENTUM_SHADOW_RULE_VERSION
        ):
            run_id = str(run["runId"])
            payload["auditRunId"] = run_id
            payload["cacheHit"] = True
            return run_id
    run_id = ledger.record_automation_run(
        run_type="momentum-shadow",
        status="succeeded",
        stage="evaluated",
        started_at=observed_at,
        finished_at=observed_at,
        details=payload,
    )
    payload["auditRunId"] = run_id
    payload["cacheHit"] = False
    return run_id


def _momentum_shadow_recorded(
    ledger: Any, *, session_date: str
) -> bool:
    return any(
        run.get("status") == "succeeded"
        and isinstance(run.get("details"), Mapping)
        and run["details"].get("sessionDate") == session_date
        and run["details"].get("ruleVersion") == MOMENTUM_SHADOW_RULE_VERSION
        for run in ledger.recent_automation_runs(
            limit=100, run_type="momentum-shadow"
        )
    )


def _recorded_momentum_symbols(
    ledger: Any, *, session_date: str
) -> tuple[str, ...]:
    for run in ledger.recent_automation_runs(
        limit=100, run_type="momentum-shadow"
    ):
        details = run.get("details")
        if (
            run.get("status") != "succeeded"
            or not isinstance(details, Mapping)
            or details.get("sessionDate") != session_date
            or details.get("ruleVersion") != MOMENTUM_SHADOW_RULE_VERSION
        ):
            continue
        selected = details.get("selected")
        if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)):
            return ()
        return tuple(
            dict.fromkeys(
                str(row.get("symbol") or "")
                for row in selected
                if isinstance(row, Mapping) and row.get("symbol")
            )
        )
    return ()


def _momentum_collection_symbols(
    *,
    research_pool: Sequence[str],
    benchmark_symbols: Sequence[str],
    evaluation_due: bool,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *benchmark_symbols,
                *(research_pool if evaluation_due else ()),
            )
        )
    )


def _collect_intraday_sample(
    collector: MarketCollector,
    *,
    cycle_symbols: tuple[str, ...],
    collection_symbols: tuple[str, ...],
    extra_symbols: Sequence[str] = (),
    extra_count: int = SESSION_MINUTE_FETCH_COUNT,
) -> dict[str, Any]:
    target_symbols = tuple(dict.fromkeys((*collection_symbols, *extra_symbols)))
    cycle_set = frozenset(cycle_symbols)
    extra_set = frozenset(extra_symbols)
    background_symbols = tuple(
        symbol for symbol in target_symbols if symbol not in cycle_set
    )
    received = 0
    upserted = 0
    failures: list[dict[str, str]] = []
    for symbol in background_symbols:
        count = extra_count if symbol in extra_set else INTRADAY_SAMPLE_CANDLE_COUNT
        try:
            result = collector.collect(
                symbol=symbol,
                interval="1m",
                count=count,
            )
            received += result.received
            upserted += result.upserted
        except (OSError, RuntimeError, TossApiError, TypeError, ValueError) as error:
            failures.append(
                {"symbol": symbol, "error": f"{type(error).__name__}: {error}"}
            )
    return {
        "targetSymbols": list(target_symbols),
        "cycleSymbols": list(cycle_symbols),
        "backgroundSymbols": list(background_symbols),
        "requestedPerBackgroundSymbol": INTRADAY_SAMPLE_CANDLE_COUNT,
        "requestedPerExtraSymbol": extra_count,
        "extraSymbols": [symbol for symbol in extra_symbols if symbol not in cycle_set],
        "receivedCandles": received,
        "upsertedCandles": upserted,
        "failures": failures,
    }


def _collect_session_minutes(
    collector: MarketCollector,
    symbols: Sequence[str],
    *,
    count: int,
) -> None:
    for symbol in dict.fromkeys(symbols):
        try:
            collector.collect(symbol=symbol, interval="1m", count=count)
        except (OSError, RuntimeError, TossApiError, TypeError, ValueError):
            continue


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
        service = MetricsService(
            store,
            initial_cash=settings.paper_initial_cash,
            session_lookup=_optional_kr_session_lookup(settings),
        )
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
    if isinstance(value, date):
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
