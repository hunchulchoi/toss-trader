from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from .market_data import CollectionResult
from .models import Candle
from .repository import MarketRepository


class MarketRegime(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"


@dataclass(frozen=True, slots=True)
class MarketAnalysis:
    symbol: str
    regime: MarketRegime
    as_of: datetime
    close_price: Decimal
    ma20: Decimal
    ma60: Decimal
    momentum_20d: Decimal
    volume_ratio: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    symbol: str
    as_of: datetime
    close_price: Decimal
    ma20: Decimal
    ma60: Decimal
    momentum_20d: Decimal
    volume_ratio: Decimal
    score: Decimal
    currency: str
    reason: str


@dataclass(frozen=True, slots=True)
class MarketScanResult:
    markets: tuple[MarketAnalysis, ...]
    candidates: tuple[DiscoveryCandidate, ...]
    errors: dict[str, str]
    names: dict[str, str]


class DailyCollector(Protocol):
    def collect_symbol_names(self, symbols: tuple[str, ...]) -> dict[str, str]: ...

    def collect(
        self,
        *,
        symbol: str,
        interval: str,
        count: int = 100,
        before: str | None = None,
        adjusted: bool = True,
    ) -> CollectionResult: ...


class MarketScanner:
    def __init__(
        self, *, collector: DailyCollector, repository: MarketRepository
    ) -> None:
        self._collector = collector
        self._repository = repository

    def run(
        self,
        *,
        benchmark_symbols: tuple[str, ...],
        discovery_symbols: tuple[str, ...],
        top_n: int,
    ) -> MarketScanResult:
        if not benchmark_symbols:
            raise ValueError("market benchmarks must not be empty")
        if not discovery_symbols:
            raise ValueError("discovery universe must not be empty")
        if not 1 <= top_n <= 50:
            raise ValueError("discovery top_n must be between 1 and 50")

        errors: dict[str, str] = {}
        all_symbols = tuple(dict.fromkeys((*benchmark_symbols, *discovery_symbols)))
        try:
            names = self._collector.collect_symbol_names(all_symbols)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            names = {}
            errors["stock_info"] = str(error)
        for symbol in all_symbols:
            try:
                self._collector.collect(symbol=symbol, interval="1d", count=60)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                errors[symbol] = str(error)

        markets: list[MarketAnalysis] = []
        for symbol in benchmark_symbols:
            if symbol in errors:
                continue
            try:
                markets.append(
                    analyze_market(
                        self._repository.latest_candles(symbol, "1d", limit=60)
                    )
                )
            except (TypeError, ValueError) as error:
                errors[symbol] = str(error)

        candidates: list[DiscoveryCandidate] = []
        for symbol in discovery_symbols:
            if symbol in errors:
                continue
            try:
                candidate = discover_candidate(
                    self._repository.latest_candles(symbol, "1d", limit=60)
                )
                if candidate is not None:
                    candidates.append(candidate)
            except (TypeError, ValueError) as error:
                errors[symbol] = str(error)

        candidates.sort(key=lambda item: (-item.score, item.symbol))
        return MarketScanResult(
            markets=tuple(markets),
            candidates=tuple(candidates[:top_n]),
            errors=errors,
            names=names,
        )


def analyze_market(candles: list[Candle]) -> MarketAnalysis:
    _validate_daily_candles(candles)
    close, ma20, ma60, momentum, volume_ratio = _indicators(candles)
    if close > ma20 > ma60 and momentum > 0:
        regime = MarketRegime.RISK_ON
    elif close < ma20 < ma60 and momentum < 0:
        regime = MarketRegime.RISK_OFF
    else:
        regime = MarketRegime.NEUTRAL
    latest = candles[-1]
    return MarketAnalysis(
        symbol=latest.symbol,
        regime=regime,
        as_of=latest.timestamp,
        close_price=close,
        ma20=ma20,
        ma60=ma60,
        momentum_20d=momentum,
        volume_ratio=volume_ratio,
        currency=latest.currency,
    )


def discover_candidate(candles: list[Candle]) -> DiscoveryCandidate | None:
    _validate_daily_candles(candles)
    close, ma20, ma60, momentum, volume_ratio = _indicators(candles)
    if not (close > ma20 > ma60 and momentum > 0):
        return None
    latest = candles[-1]
    score = _rounded(momentum * Decimal(100) + min(volume_ratio, Decimal(5)))
    return DiscoveryCandidate(
        symbol=latest.symbol,
        as_of=latest.timestamp,
        close_price=close,
        ma20=ma20,
        ma60=ma60,
        momentum_20d=momentum,
        volume_ratio=volume_ratio,
        score=score,
        currency=latest.currency,
        reason="close > MA20 > MA60 and positive 20d momentum",
    )


def market_scan_to_dict(result: MarketScanResult) -> dict[str, object]:
    return {
        "markets": [
            {
                "symbol": item.symbol,
                "name": result.names.get(item.symbol, item.symbol),
                "regime": item.regime.value,
                "asOf": item.as_of,
                "closePrice": item.close_price,
                "ma20": item.ma20,
                "ma60": item.ma60,
                "momentum20d": item.momentum_20d,
                "volumeRatio": item.volume_ratio,
                "currency": item.currency,
            }
            for item in result.markets
        ],
        "candidates": [
            {
                "symbol": item.symbol,
                "name": result.names.get(item.symbol, item.symbol),
                "asOf": item.as_of,
                "closePrice": item.close_price,
                "ma20": item.ma20,
                "ma60": item.ma60,
                "momentum20d": item.momentum_20d,
                "volumeRatio": item.volume_ratio,
                "score": item.score,
                "currency": item.currency,
                "reason": item.reason,
            }
            for item in result.candidates
        ],
        "errors": result.errors,
    }


def format_market_scan_report(
    process_result: dict[str, object], *, opinion: str
) -> str:
    opinion = opinion.strip()
    if not opinion:
        raise ValueError("LLM opinion must not be empty")
    scan = process_result.get("scan")
    scan = scan if isinstance(scan, dict) else {}
    markets = scan.get("markets") if isinstance(scan.get("markets"), list) else []
    candidates = (
        scan.get("candidates") if isinstance(scan.get("candidates"), list) else []
    )
    errors = scan.get("errors") if isinstance(scan.get("errors"), dict) else {}

    market_lines = [
        f"• {_market_label(item)}: {item.get('regime', 'UNKNOWN')}\n"
        f"  20일 모멘텀 {_percent(item.get('momentum20d'))}"
        for item in markets
        if isinstance(item, dict)
    ]
    candidate_lines = [
        f"{index}. {_candidate_label(item)}\n"
        f"   모멘텀 {_percent(item.get('momentum20d'))}\n"
        f"   거래량 {_ratio(item.get('volumeRatio'))}\n"
        f"   점수 {item.get('score', '?')}"
        for index, item in enumerate(
            (item for item in candidates if isinstance(item, dict)), start=1
        )
    ]
    return (
        "📊 시장 분석\n"
        f"{'\n\n'.join(market_lines) if market_lines else '분석 결과 없음'}\n\n"
        "🔎 발굴 종목\n"
        f"{'\n\n'.join(candidate_lines) if candidate_lines else '조건 충족 종목 없음'}\n\n"
        "💬 Hermes 의견\n"
        f"{opinion[:1500]}\n\n"
        f"오류 {len(errors)}건"
    )


def _market_label(item: dict[str, object]) -> str:
    symbol = str(item.get("symbol", "?"))
    return str(item.get("name") or symbol)


def _candidate_label(item: dict[str, object]) -> str:
    symbol = str(item.get("symbol", "?"))
    name = str(item.get("name") or symbol)
    return f"{name} ({symbol})" if name != symbol else symbol


def _validate_daily_candles(candles: list[Candle]) -> None:
    if len(candles) < 60:
        raise ValueError(f"need 60 candles, found {len(candles)}")
    latest = candles[-60:]
    if any(item.interval != "1d" for item in latest):
        raise ValueError("market scan requires daily candles")
    if len({item.symbol for item in latest}) != 1:
        raise ValueError("market scan candles must share one symbol")


def _indicators(
    candles: list[Candle],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    latest = candles[-60:]
    closes = [item.close_price for item in latest]
    close = closes[-1]
    ma20 = _rounded(sum(closes[-20:], Decimal(0)) / Decimal(20))
    ma60 = _rounded(sum(closes, Decimal(0)) / Decimal(60))
    momentum = _rounded(close / closes[-21] - Decimal(1))
    previous_volumes = [item.volume for item in latest[-21:-1]]
    average_volume = sum(previous_volumes, Decimal(0)) / Decimal(20)
    volume_ratio = (
        _rounded(latest[-1].volume / average_volume)
        if average_volume > 0
        else Decimal(0)
    )
    return close, ma20, ma60, momentum, volume_ratio


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _percent(value: object) -> str:
    try:
        percent = Decimal(str(value)) * Decimal(100)
    except Exception:  # noqa: BLE001
        return "?"
    return f"{percent:+.2f}%"


def _ratio(value: object) -> str:
    try:
        ratio = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return "?"
    return f"{ratio:.2f}x"
