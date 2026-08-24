from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol

from .calendar import MarketSession
from .models import Candle

RULE_VERSION = "momentum-shadow-v2"
SELECTION_TIME = time(10, 0)
OPENING_SPIKE_LIMIT = Decimal("0.08")
ASCENT_THRESHOLD = Decimal("0.03")
PULLBACK_MIN = Decimal("0.01")
PULLBACK_MAX = Decimal("0.04")
OPEN_RETENTION_MIN = Decimal("0.01")
RECLAIM_HOLD_FLOOR = Decimal("0.995")
STOP_DISTANCE_MAX = Decimal("0.03")
REWARD_MULTIPLE = Decimal("1.5")
MAX_SELECTED = 2
SESSION_LIMIT = 400
MARKET_PROXIES = {"KOSPI": "069500", "KOSDAQ": "229200"}


class CandleReader(Protocol):
    def latest_candles(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[Candle]: ...


def ranking_symbols(
    payload: Mapping[str, Any],
    *,
    allowed_symbols: frozenset[str],
    limit: int = 30,
) -> tuple[str, ...]:
    rows = payload.get("rankings")
    if not isinstance(rows, list):
        raise TypeError("momentum rankings must contain a rankings array")
    symbols: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("each momentum ranking must be an object")
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise TypeError("momentum ranking symbol is missing")
        normalized = symbol.strip().upper()
        if normalized in allowed_symbols and normalized not in symbols:
            symbols.append(normalized)
        if len(symbols) == limit:
            break
    return tuple(symbols)


def evaluate_momentum_shadow(
    repository: CandleReader,
    *,
    symbols: Sequence[str],
    market_by_symbol: Mapping[str, str],
    session: MarketSession,
    observed_at: datetime,
) -> dict[str, Any]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("momentum shadow observed_at must include timezone")
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
    selection_at = session.market_open_at.replace(
        hour=SELECTION_TIME.hour, minute=SELECTION_TIME.minute
    )
    cutoff_at = selection_at - timedelta(minutes=1)
    entry_at = selection_at + timedelta(minutes=1)
    if observed_at < selection_at:
        return {
            "status": "waiting",
            "ruleVersion": RULE_VERSION,
            "sessionDate": session.business_date.isoformat(),
            "selectionAt": selection_at.isoformat(),
        }
    proxy_rows = {
        market: _session_rows(
            repository,
            symbol=proxy,
            session_open=session.market_open_at,
            through=selection_at,
        )
        for market, proxy in MARKET_PROXIES.items()
    }
    reasons: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for symbol in dict.fromkeys(symbols):
        market = _normalized_market(market_by_symbol.get(symbol))
        if market is None:
            reasons["missing-market"] += 1
            continue
        rows = _session_rows(
            repository,
            symbol=symbol,
            session_open=session.market_open_at,
            through=entry_at,
        )
        candidate, reason = _candidate(
            symbol=symbol,
            market=market,
            rows=rows,
            proxy_rows=proxy_rows.get(market, ()),
            cutoff_at=cutoff_at,
            entry_at=entry_at,
        )
        reasons[reason] += 1
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(
        key=lambda row: (-Decimal(row["score"]), -Decimal(row["retention"]), row["symbol"])
    )
    selected = candidates[:MAX_SELECTED]
    unique_symbol_count = len(tuple(dict.fromkeys(symbols)))
    acquisition_errors = sum(
        reasons[reason]
        for reason in ("incomplete-1m", "missing-market", "incomplete-market-proxy")
    )
    status = (
        "incomplete-data"
        if unique_symbol_count > 0
        and acquisition_errors == unique_symbol_count
        else "evaluated"
    )
    return {
        "status": status,
        "ruleVersion": RULE_VERSION,
        "strategyInput": False,
        "shadowOnly": True,
        "sessionDate": session.business_date.isoformat(),
        "observedAt": observed_at.isoformat(),
        "selectionAt": selection_at.isoformat(),
        "cutoffAt": cutoff_at.isoformat(),
        "evaluatedSymbols": unique_symbol_count,
        "candidateCount": len(candidates),
        "selectedCount": len(selected),
        "reasons": dict(sorted(reasons.items())),
        "selected": selected,
    }


def evaluate_momentum_shadow_outcome(
    candidate: Mapping[str, Any], candles: Sequence[Candle]
) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "")
    entry_at = _aware_datetime(candidate.get("entryAt"), "entryAt")
    entry_price = _positive_decimal(candidate.get("entryPrice"), "entryPrice")
    stop_price = _positive_decimal(candidate.get("stopPrice"), "stopPrice")
    target_price = _positive_decimal(candidate.get("targetPrice"), "targetPrice")
    if not symbol or not stop_price < entry_price < target_price:
        raise ValueError("invalid momentum shadow trade plan")
    rows = sorted(
        (
            candle
            for candle in candles
            if candle.symbol == symbol
            and candle.interval == "1m"
            and candle.timestamp >= entry_at
        ),
        key=lambda candle: candle.timestamp,
    )
    if not rows:
        return {
            "status": "waiting-data",
            "symbol": symbol,
            "entryAt": entry_at.isoformat(),
            "entryPrice": str(entry_price),
        }
    exit_row = rows[-1]
    exit_price = exit_row.close_price
    status = "marked"
    for row in rows:
        stop_hit = row.low_price <= stop_price
        target_hit = row.high_price >= target_price
        if stop_hit:
            status = "stopped"
            exit_row = row
            exit_price = stop_price
            break
        if target_hit:
            status = "target"
            exit_row = row
            exit_price = target_price
            break
    risk = entry_price - stop_price
    return_rate = exit_price / entry_price - Decimal(1)
    maximum_high = max(row.high_price for row in rows)
    minimum_low = min(row.low_price for row in rows)
    return {
        "status": status,
        "symbol": symbol,
        "entryAt": entry_at.isoformat(),
        "entryPrice": str(entry_price),
        "exitAt": exit_row.timestamp.isoformat(),
        "exitPrice": str(exit_price),
        "returnRate": str(return_rate),
        "rMultiple": str((exit_price - entry_price) / risk),
        "maximumFavorableRate": str(maximum_high / entry_price - Decimal(1)),
        "maximumAdverseRate": str(minimum_low / entry_price - Decimal(1)),
    }


def _session_rows(
    repository: CandleReader,
    *,
    symbol: str,
    session_open: datetime,
    through: datetime,
) -> list[Candle]:
    rows = [
        candle
        for candle in repository.latest_candles(symbol, "1m", limit=SESSION_LIMIT)
        if session_open < candle.timestamp <= through
    ]
    return sorted(rows, key=lambda candle: candle.timestamp)


def _candidate(
    *,
    symbol: str,
    market: str,
    rows: Sequence[Candle],
    proxy_rows: Sequence[Candle],
    cutoff_at: datetime,
    entry_at: datetime,
) -> tuple[dict[str, Any] | None, str]:
    expected_cutoff = _row_at(rows, cutoff_at)
    entry = _row_at(rows, entry_at)
    if expected_cutoff is None or entry is None or len(rows) < 60:
        return None, "incomplete-1m"
    at_0905 = _row_at(rows, entry_at.replace(hour=9, minute=5))
    if at_0905 is None:
        return None, "incomplete-1m"
    session_open = rows[0].open_price
    if at_0905.close_price / session_open - Decimal(1) > OPENING_SPIKE_LIMIT:
        return None, "opening-spike"
    peak_close = session_open
    pullback_low: Decimal | None = None
    pullback_index: int | None = None
    confirmed_index: int | None = None
    state = "ascent"
    cutoff_index = rows.index(expected_cutoff)
    for index, candle in enumerate(rows[: cutoff_index + 1]):
        if index < 14:
            peak_close = max(peak_close, candle.close_price)
            continue
        if state == "ascent":
            peak_close = max(peak_close, candle.close_price)
            if peak_close / session_open - Decimal(1) < ASCENT_THRESHOLD:
                continue
            drawdown = Decimal(1) - candle.close_price / peak_close
            if (
                PULLBACK_MIN <= drawdown <= PULLBACK_MAX
                and candle.close_price / session_open - Decimal(1)
                >= OPEN_RETENTION_MIN
            ):
                state = "pullback"
                pullback_index = index
                pullback_low = candle.low_price
        elif state == "pullback":
            assert pullback_low is not None
            pullback_low = min(pullback_low, candle.low_price)
            drawdown = Decimal(1) - candle.close_price / peak_close
            if (
                drawdown > PULLBACK_MAX
                or candle.close_price / session_open - Decimal(1)
                < OPEN_RETENTION_MIN
            ):
                return None, "deep-pullback"
            if index > (pullback_index or 0) and candle.close_price >= peak_close:
                confirmed_index = index
                state = "confirmed"
        else:
            assert pullback_low is not None
            pullback_low = min(pullback_low, candle.low_price)
    if confirmed_index is None or pullback_low is None:
        return None, "no-reclaim"
    if confirmed_index + 3 > cutoff_index:
        return None, "insufficient-hold-bars"
    reclaimed_peak = max(candle.close_price for candle in rows[: confirmed_index + 1])
    if any(
        rows[index].close_price < reclaimed_peak * RECLAIM_HOLD_FLOOR
        for index in range(confirmed_index + 1, confirmed_index + 4)
    ):
        return None, "reclaim-not-held-3-bars"
    if expected_cutoff.close_price < peak_close * RECLAIM_HOLD_FLOOR:
        return None, "reclaim-not-held"
    market_alignment = _market_alignment(proxy_rows, cutoff_at)
    if market_alignment is None:
        return None, "incomplete-market-proxy"
    if not market_alignment:
        return None, "market-not-aligned"
    entry_price = entry.open_price
    stop_price = pullback_low
    stop_distance = Decimal(1) - stop_price / entry_price
    if stop_distance <= 0 or stop_distance > STOP_DISTANCE_MAX:
        return None, "stop-distance"
    recent = rows[max(0, cutoff_index - 4) : cutoff_index + 1]
    previous = rows[max(0, cutoff_index - 14) : max(0, cutoff_index - 4)]
    recent_value = sum(
        (candle.close_price * candle.volume for candle in recent), Decimal(0)
    )
    previous_value = sum(
        (candle.close_price * candle.volume for candle in previous), Decimal(0)
    )
    acceleration = (
        (recent_value / len(recent)) / (previous_value / len(previous))
        if previous and previous_value > 0
        else Decimal(0)
    )
    session_high = max(candle.high_price for candle in rows[: cutoff_index + 1])
    retention = expected_cutoff.close_price / session_high
    risk_per_share = entry_price - stop_price
    return (
        {
            "symbol": symbol,
            "market": market,
            "score": str(acceleration * retention),
            "acceleration": str(acceleration),
            "retention": str(retention),
            "confirmedAt": rows[confirmed_index].timestamp.isoformat(),
            "entryAt": entry.timestamp.isoformat(),
            "entryPrice": str(entry_price),
            "stopPrice": str(stop_price),
            "targetPrice": str(entry_price + risk_per_share * REWARD_MULTIPLE),
            "stopDistance": str(stop_distance),
            "rewardMultiple": str(REWARD_MULTIPLE),
        },
        "candidate",
    )


def _market_alignment(
    rows: Sequence[Candle], cutoff_at: datetime
) -> bool | None:
    cutoff = _row_at(rows, cutoff_at)
    if cutoff is None:
        return None
    index = rows.index(cutoff)
    if index < 5:
        return None
    return (
        cutoff.close_price > rows[0].open_price
        and cutoff.close_price > rows[index - 5].close_price
    )


def _row_at(rows: Sequence[Candle], timestamp: datetime) -> Candle | None:
    return next((row for row in rows if row.timestamp == timestamp), None)


def _normalized_market(value: object) -> str | None:
    text = str(value or "").upper()
    if "KOSDAQ" in text:
        return "KOSDAQ"
    if "KOSPI" in text or text in {"KSE", "STK"}:
        return "KOSPI"
    return None


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"momentum shadow {field} is required")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"momentum shadow {field} must include timezone")
    return parsed


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as error:
        raise TypeError(f"momentum shadow {field} must be numeric") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"momentum shadow {field} must be positive")
    return parsed
