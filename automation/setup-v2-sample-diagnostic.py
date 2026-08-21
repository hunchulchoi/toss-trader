from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import mean
from zoneinfo import ZoneInfo

import psycopg

from toss_trader.models import Candle
from toss_trader.setup_screening import evaluate_price_setups

SEOUL = ZoneInfo("Asia/Seoul")
STATIC_VIOLATIONS = (
    "unsupported-security-type",
    "not-common-share",
    "stock-not-active",
    "trading-suspended",
    "invalid-reference-price",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a read-only price setup sample diagnostic"
    )
    parser.add_argument(
        "--sessions",
        nargs="+",
        required=True,
        type=date.fromisoformat,
    )
    return parser


def local_date(value: datetime) -> date:
    return value.astimezone(SEOUL).date()


def main() -> int:
    args = build_parser().parse_args()
    sessions = tuple(sorted(set(args.sessions)))
    connection = psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        options="-c default_transaction_read_only=on",
    )
    try:
        payload = diagnose(connection, sessions=sessions)
    finally:
        connection.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def diagnose(connection: psycopg.Connection, *, sessions: tuple[date, ...]) -> dict:
    cutoff = datetime.combine(max(sessions) + timedelta(days=1), time(0), tzinfo=SEOUL)
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT symbol, interval, timestamp, open_price, high_price,
                      low_price, close_price, volume, currency
               FROM market_candles
               WHERE interval='1d' AND timestamp < %s
               ORDER BY symbol, timestamp""",
            (cutoff,),
        )
        daily_rows = cursor.fetchall()
        cursor.execute("SELECT count(*) FROM market_symbols")
        known_count = int(cursor.fetchone()[0])
        cursor.execute(
            """SELECT d.symbol
               FROM dynamic_universe_decisions d
               WHERE d.run_id = (
                 SELECT run_id FROM dynamic_universe_runs
                 WHERE status='succeeded'
                   AND ranking_source='official:d1-known-pool'
                 ORDER BY evaluated_at DESC LIMIT 1
               )
               AND NOT d.violations ?| %s""",
            (list(STATIC_VIOLATIONS),),
        )
        static_eligible = {str(row[0]) for row in cursor.fetchall()}
    if not static_eligible:
        raise RuntimeError("no successful setup-first static snapshot")

    daily: dict[str, list[Candle]] = defaultdict(list)
    date_counts: Counter[date] = Counter()
    for row in daily_rows:
        candle = Candle(
            symbol=str(row[0]),
            interval=str(row[1]),
            timestamp=row[2],
            open_price=Decimal(row[3]),
            high_price=Decimal(row[4]),
            low_price=Decimal(row[5]),
            close_price=Decimal(row[6]),
            volume=Decimal(row[7]),
            currency=str(row[8]),
        )
        daily[candle.symbol].append(candle)
        date_counts[local_date(candle.timestamp)] += 1
    reliable_dates = tuple(
        sorted(day for day, count in date_counts.items() if count >= 100)
    )

    reports = []
    unique_setup_symbols: set[str] = set()
    for session_day in sessions:
        prior_dates = [day for day in reliable_dates if day < session_day]
        if not prior_dates:
            raise RuntimeError(f"no reliable prior session for {session_day}")
        signal_day = max(prior_dates)
        setups: dict[str, object] = {}
        setup_types: Counter[str] = Counter()
        evaluable = 0
        insufficient = 0
        stale = 0
        for symbol in static_eligible:
            history = [
                item
                for item in daily.get(symbol, ())
                if local_date(item.timestamp) <= signal_day
            ]
            if len(history) < 200:
                insufficient += 1
                continue
            if local_date(history[-1].timestamp) != signal_day:
                stale += 1
                continue
            evaluable += 1
            evidence = evaluate_price_setups(history[-200:])
            if not evidence.setups:
                continue
            setups[symbol] = evidence
            unique_setup_symbols.add(symbol)
            setup_types.update(item.value for item in evidence.setups)

        intraday: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
        if setups:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT symbol, open_price, close_price
                       FROM market_candles
                       WHERE interval='1m'
                         AND symbol = ANY(%s)
                         AND timestamp >= %s AND timestamp <= %s
                       ORDER BY symbol, timestamp""",
                    (
                        list(setups),
                        datetime.combine(session_day, time(9, 1), tzinfo=SEOUL),
                        datetime.combine(session_day, time(15, 30), tzinfo=SEOUL),
                    ),
                )
                for symbol, open_price, close_price in cursor.fetchall():
                    intraday[str(symbol)].append(
                        (Decimal(open_price), Decimal(close_price))
                    )

        returns = [
            float(bars[-1][1] / bars[0][0] - Decimal(1))
            for symbol in setups
            if (bars := intraday.get(symbol))
        ]
        reports.append(
            {
                "sessionDate": session_day,
                "signalDate": signal_day,
                "knownSymbols": known_count,
                "currentStaticEligibleSymbols": len(static_eligible),
                "evaluable": evaluable,
                "insufficientHistory": insufficient,
                "staleHistory": stale,
                "priceSetupCount": len(setups),
                "setupTypes": dict(sorted(setup_types.items())),
                "intradayAny": len(returns),
                "intradayComplete": sum(
                    len(intraday.get(symbol, ())) >= 390 for symbol in setups
                ),
                "positiveOpenToClose": sum(item > 0 for item in returns),
                "meanOpenToCloseReturn": mean(returns) if returns else None,
            }
        )

    return {
        "mode": "price-only-counterfactual",
        "strictSetupV2Approved": False,
        "pnlEvaluated": False,
        "survivorshipBias": "current-known-pool-and-current-static-metadata",
        "sessions": reports,
        "uniquePriceSetupSymbols": len(unique_setup_symbols),
    }


if __name__ == "__main__":
    raise SystemExit(main())
