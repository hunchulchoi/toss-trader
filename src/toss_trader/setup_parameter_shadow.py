from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .calendar import MarketSession
from .models import Candle
from .setup_screening import (
    DEFAULT_POSITION_SIZING_POLICY,
    PositionSizingPolicy,
    SetupType,
    evaluate_price_setups,
    position_size_reference,
)
from .v2_engine import ADVERSE_SLIPPAGE, wilder_atr

SEOUL = ZoneInfo("Asia/Seoul")
RULE_VERSION = "setup-parameter-shadow-v1"
TIGHT_PULLBACK_DISTANCE = Decimal("0.02")
ALTERNATE_OVERSOLD_RSI = Decimal(40)
GAP_VARIANTS = (Decimal("0.02"), Decimal("0.03"))
ENTRY_MINUTES = (5, 10, 15, 20, 25, 30)
ONE_PERCENT_RISK_POLICY = PositionSizingPolicy(
    per_trade_risk_rate=Decimal("0.01"),
    max_open_heat_rate=Decimal("0.02"),
    max_cluster_heat_rate=Decimal("0.02"),
    max_order_notional=DEFAULT_POSITION_SIZING_POLICY.max_order_notional,
    atr_stop_multiple=DEFAULT_POSITION_SIZING_POLICY.atr_stop_multiple,
)
ATR_ONE_POLICY = PositionSizingPolicy(
    per_trade_risk_rate=DEFAULT_POSITION_SIZING_POLICY.per_trade_risk_rate,
    max_open_heat_rate=DEFAULT_POSITION_SIZING_POLICY.max_open_heat_rate,
    max_cluster_heat_rate=DEFAULT_POSITION_SIZING_POLICY.max_cluster_heat_rate,
    max_order_notional=DEFAULT_POSITION_SIZING_POLICY.max_order_notional,
    atr_stop_multiple=Decimal("1.0"),
)
ONE_PERCENT_ATR_ONE_POLICY = PositionSizingPolicy(
    per_trade_risk_rate=Decimal("0.01"),
    max_open_heat_rate=Decimal("0.02"),
    max_cluster_heat_rate=Decimal("0.02"),
    max_order_notional=DEFAULT_POSITION_SIZING_POLICY.max_order_notional,
    atr_stop_multiple=Decimal("1.0"),
)


class CandleReader(Protocol):
    def latest_candles(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[Candle]: ...


def evaluate_setup_parameter_shadow(
    repository: CandleReader,
    *,
    symbols: Sequence[str],
    session: MarketSession,
    signal_session: date,
    observed_at: datetime,
    equity: Decimal = Decimal(1000000),
    available_cash: Decimal = Decimal(1000000),
) -> dict[str, Any]:
    """Evaluate research-only setup thresholds from persisted D-1 and 1m facts."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("parameter shadow observed_at must include timezone")
    if equity <= 0 or available_cash < 0:
        raise ValueError("parameter shadow capital values are invalid")
    if signal_session >= session.business_date:
        raise ValueError("parameter shadow signal session must precede session")
    if (
        not session.is_business_day
        or session.market_open_at is None
        or session.market_close_at is None
    ):
        return {
            "status": "closed",
            "ruleVersion": RULE_VERSION,
            "sessionDate": session.business_date.isoformat(),
        }
    required_through = session.market_open_at + timedelta(minutes=30)
    if observed_at < required_through:
        return {
            "status": "waiting",
            "ruleVersion": RULE_VERSION,
            "sessionDate": session.business_date.isoformat(),
            "requiredThrough": required_through.isoformat(),
        }

    reasons: Counter[str] = Counter()
    aggregates: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    daily_evaluated = 0
    opening_complete = 0
    unique_symbols = tuple(dict.fromkeys(symbols))
    for symbol in unique_symbols:
        daily = _completed_daily(
            repository,
            symbol=symbol,
            signal_session=signal_session,
        )
        minute_rows = _session_rows(
            repository,
            symbol=symbol,
            session=session,
            observed_at=observed_at,
        )
        opening_rows = tuple(
            row
            for minute in range(1, 31)
            if (
                row := _row_at(
                    minute_rows,
                    session.market_open_at + timedelta(minutes=minute),
                )
            )
            is not None
        )
        first_bar = opening_rows[0] if len(opening_rows) == 30 else None
        if len(opening_rows) == 30:
            opening_complete += 1
        else:
            reasons["incomplete-opening-1m"] += 1
        if not daily:
            reasons["missing-daily"] += 1
            continue
        if daily[-1].timestamp.astimezone(SEOUL).date() != signal_session:
            reasons["stale-daily"] += 1
            continue
        if len(daily) < 200:
            reasons["insufficient-daily"] += 1
            continue
        daily_evaluated += 1
        history = daily[-200:]
        evidence = evaluate_price_setups(history)
        latest = history[-1]
        previous = history[-2]
        current_pullback = SetupType.PULLBACK in evidence.setups
        current_oversold = SetupType.OVERSOLD_REVERSAL in evidence.setups
        tight_pullback = (
            current_pullback
            and evidence.ma50_distance <= TIGHT_PULLBACK_DISTANCE
        )
        alternate_oversold = (
            evidence.rsi14 <= ALTERNATE_OVERSOLD_RSI
            and latest.close_price > latest.open_price
            and latest.close_price > previous.close_price
        )
        current_price_setup = current_pullback or current_oversold
        if current_pullback:
            aggregates["currentPullback4Pct"] += 1
        if current_oversold:
            aggregates["currentOversoldRsi35PreviousHigh"] += 1
        if tight_pullback:
            aggregates["tightPullback2Pct"] += 1
        if alternate_oversold:
            aggregates["alternateOversoldRsi40PreviousClose"] += 1
        if current_price_setup:
            aggregates["currentPriceSetup"] += 1
        if not (current_price_setup or tight_pullback or alternate_oversold):
            continue

        row: dict[str, Any] = {
            "symbol": symbol,
            "signalSession": signal_session.isoformat(),
            "dailyEvidenceHash": _daily_hash(history),
            "signalClose": str(latest.close_price),
            "setupLow": str(latest.low_price),
            "rsi14": str(evidence.rsi14),
            "ma50": str(evidence.ma50),
            "ma200": str(evidence.ma200),
            "ma50Distance": str(evidence.ma50_distance),
            "currentPullback4Pct": current_pullback,
            "currentOversoldRsi35PreviousHigh": current_oversold,
            "tightPullback2Pct": tight_pullback,
            "alternateOversoldRsi40PreviousClose": alternate_oversold,
            "currentPriceSetup": current_price_setup,
            "openingBarsComplete": len(opening_rows) == 30,
            "openingBarCount": len(opening_rows),
        }
        if first_bar is None:
            rows.append(row)
            continue
        row["openingEvidenceHash"] = _candle_hash(opening_rows)
        gap = first_bar.open_price / latest.close_price - Decimal(1)
        row["gapRate"] = str(gap)
        for threshold in GAP_VARIANTS:
            passed = gap < threshold
            key = f"gap{int(threshold * 100)}PctPass"
            row[key] = passed
            if current_price_setup and passed:
                aggregates[key] += 1
            if alternate_oversold and passed:
                aggregates[f"alternateOversold_{key}"] += 1

        first_valid = _first_valid_entry(
            minute_rows,
            session_open_at=session.market_open_at,
            setup_low=latest.low_price,
            opening_price=first_bar.open_price,
        )
        if first_valid is None:
            row["firstValidAt"] = None
            rows.append(row)
            continue
        row["firstValidAt"] = first_valid.timestamp.isoformat()
        gap3_armable = gap < Decimal("0.03")
        if gap3_armable and (current_price_setup or alternate_oversold):
            atr14 = wilder_atr(history)
            policies = {
                "ruleRisk0_5Atr1_5": DEFAULT_POSITION_SIZING_POLICY,
                "risk1Atr1_5": ONE_PERCENT_RISK_POLICY,
                "risk0_5Atr1": ATR_ONE_POLICY,
                "risk1Atr1": ONE_PERCENT_ATR_ONE_POLICY,
            }
            quantities: dict[str, Decimal] = {}
            sizing_errors: dict[str, str] = {}
            for key, policy in policies.items():
                try:
                    quantities[key] = _quantity(
                        symbol=symbol,
                        reference_price=first_valid.close_price,
                        setup_low=latest.low_price,
                        atr14=atr14,
                        equity=equity,
                        available_cash=available_cash,
                        policy=policy,
                    )
                except ValueError as error:
                    sizing_errors[key] = str(error)
            row["atr14"] = str(atr14)
            row["quantities"] = {key: str(value) for key, value in quantities.items()}
            if sizing_errors:
                row["sizingErrors"] = sizing_errors
                aggregates["invalidSizingReference"] += 1
            if current_price_setup:
                aggregates["currentGap3ValidStopBy0930"] += 1
                if gap < Decimal("0.02"):
                    aggregates["currentGap2ValidStopBy0930"] += 1
            if alternate_oversold:
                aggregates["alternateOversoldGap3ValidStopBy0930"] += 1
                if gap < Decimal("0.02"):
                    aggregates["alternateOversoldGap2ValidStopBy0930"] += 1
            for key, quantity in quantities.items():
                if quantity > 0 and current_price_setup:
                    aggregates[f"{key}AtLeastOne"] += 1
                    if gap < Decimal("0.02"):
                        aggregates[f"gap2_{key}AtLeastOne"] += 1
                if quantity > 0 and alternate_oversold:
                    aggregates[f"alternateOversold_{key}AtLeastOne"] += 1
                    if gap < Decimal("0.02"):
                        aggregates[f"alternateOversold_gap2_{key}AtLeastOne"] += 1
        rows.append(row)

    data_complete = (
        bool(unique_symbols)
        and opening_complete == len(unique_symbols)
        and reasons["missing-daily"] == 0
        and reasons["stale-daily"] == 0
    )
    return {
        "status": "evaluated" if data_complete else "partial-data",
        "ruleVersion": RULE_VERSION,
        "strategyInput": False,
        "shadowOnly": True,
        "strictPITApproved": False,
        "sessionDate": session.business_date.isoformat(),
        "signalSession": signal_session.isoformat(),
        "observedAt": observed_at.isoformat(),
        "sizingAssumptions": {
            "equity": str(equity),
            "availableCash": str(available_cash),
            "existingOpenHeat": "0",
            "existingClusterHeat": "0",
            "maxOrderNotional": str(
                DEFAULT_POSITION_SIZING_POLICY.max_order_notional
            ),
            "adverseSlippageEntry": str(ADVERSE_SLIPPAGE.entry_rate),
            "adverseSlippageExit": str(ADVERSE_SLIPPAGE.exit_rate),
        },
        "dataQuality": {
            "requestedSymbols": len(unique_symbols),
            "dailyEvaluated": daily_evaluated,
            "openingComplete": opening_complete,
            "openingCoverageRate": (
                str(Decimal(opening_complete) / Decimal(len(unique_symbols)))
                if unique_symbols
                else "1"
            ),
            "reasons": dict(sorted(reasons.items())),
        },
        "variants": dict(sorted(aggregates.items())),
        "rows": sorted(rows, key=lambda item: item["symbol"]),
    }


def _completed_daily(
    repository: CandleReader, *, symbol: str, signal_session: date
) -> list[Candle]:
    rows = repository.latest_candles(symbol, "1d", limit=400)
    return sorted(
        (
            row
            for row in rows
            if row.timestamp.astimezone(SEOUL).date() <= signal_session
        ),
        key=lambda row: row.timestamp,
    )


def _session_rows(
    repository: CandleReader,
    *,
    symbol: str,
    session: MarketSession,
    observed_at: datetime,
) -> list[Candle]:
    assert session.market_open_at is not None
    assert session.market_close_at is not None
    cutoff = min(observed_at, session.market_close_at)
    return sorted(
        (
            row
            for row in repository.latest_candles(symbol, "1m", limit=500)
            if session.market_open_at < row.timestamp <= cutoff
        ),
        key=lambda row: row.timestamp,
    )


def _first_valid_entry(
    rows: Sequence[Candle],
    *,
    session_open_at: datetime,
    setup_low: Decimal,
    opening_price: Decimal,
) -> Candle | None:
    if opening_price <= setup_low:
        return None
    for minute in ENTRY_MINUTES:
        row = _row_at(rows, session_open_at + timedelta(minutes=minute))
        if row is not None and row.close_price > setup_low:
            return row
    return None


def _row_at(rows: Sequence[Candle], timestamp: datetime) -> Candle | None:
    return next((row for row in rows if row.timestamp == timestamp), None)


def _quantity(
    *,
    symbol: str,
    reference_price: Decimal,
    setup_low: Decimal,
    atr14: Decimal,
    equity: Decimal,
    available_cash: Decimal,
    policy: PositionSizingPolicy,
) -> Decimal:
    return position_size_reference(
        symbol=symbol,
        equity=equity,
        reference_price=reference_price,
        stop_price=setup_low,
        atr=atr14,
        available_cash=available_cash,
        current_open_heat=Decimal(0),
        current_cluster_heat=Decimal(0),
        policy=policy,
        slippage=ADVERSE_SLIPPAGE,
    ).quantity


def _daily_hash(rows: Sequence[Candle]) -> str:
    return _candle_hash(rows)


def _candle_hash(rows: Sequence[Candle]) -> str:
    payload = "\n".join(
        "|".join(
            (
                row.symbol,
                row.timestamp.isoformat(),
                str(row.open_price),
                str(row.high_price),
                str(row.low_price),
                str(row.close_price),
                str(row.volume),
            )
        )
        for row in rows
    )
    return sha256(payload.encode()).hexdigest()
