from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .market_data import CollectionResult
from .screening import MarketAnalysis, analyze_market
from .setup_screening import hermes_experimental_can_arm
from .v2_engine import DailySetupCandidate


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


class CandidateBuilder(Protocol):
    def build_candidate(
        self, symbol: str, *, now: datetime
    ) -> DailySetupCandidate: ...


@dataclass(frozen=True, slots=True)
class V2MarketScanResult:
    markets: tuple[MarketAnalysis, ...]
    candidates: tuple[DailySetupCandidate, ...]
    scanned_count: int
    evaluated_count: int
    blocked_count: int
    blocked_reasons: dict[str, int]
    hermes_candidates: tuple[DailySetupCandidate, ...]
    hard_blocked_count: int
    hermes_hard_blocked_reasons: dict[str, int]
    errors: dict[str, str]
    names: dict[str, str]
    decision_at: datetime


class V2MarketScanner:
    def __init__(
        self,
        *,
        collector: DailyCollector,
        repository,
        candidate_builder: CandidateBuilder,
    ) -> None:
        self._collector = collector
        self._repository = repository
        self._candidate_builder = candidate_builder

    def run(
        self,
        *,
        benchmark_symbols: tuple[str, ...],
        discovery_symbols: tuple[str, ...],
        top_n: int,
        now: datetime,
    ) -> V2MarketScanResult:
        if not benchmark_symbols:
            raise ValueError("market benchmarks must not be empty")
        if not discovery_symbols:
            raise ValueError("discovery universe must not be empty")
        if not 1 <= top_n <= 50:
            raise ValueError("discovery top_n must be between 1 and 50")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("market scan time must include a timezone offset")

        errors: dict[str, str] = {}
        blocked = Counter[str]()
        hermes_hard = Counter[str]()
        all_symbols = tuple(dict.fromkeys((*benchmark_symbols, *discovery_symbols)))
        discovery_set = set(discovery_symbols)
        try:
            names = self._collector.collect_symbol_names(all_symbols)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            names = {}
            errors["stock_info"] = str(error)
        for symbol in all_symbols:
            try:
                self._collector.collect(
                    symbol=symbol,
                    interval="1d",
                    count=200 if symbol in discovery_set else 60,
                )
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

        candidates: list[DailySetupCandidate] = []
        hermes_candidates: list[DailySetupCandidate] = []
        evaluated_count = 0
        blocked_count = 0
        hard_blocked_count = 0
        for symbol in discovery_symbols:
            if symbol in errors:
                continue
            try:
                candidate = self._candidate_builder.build_candidate(symbol, now=now)
            except ValueError as error:
                message = str(error)
                if message.startswith("setup-v2:missing:"):
                    reason = message.removeprefix("setup-v2:")
                    blocked[reason] += 1
                    hermes_hard[reason] += 1
                    blocked_count += 1
                    hard_blocked_count += 1
                else:
                    errors[symbol] = message
                continue
            except (OSError, RuntimeError, TypeError) as error:
                errors[symbol] = str(error)
                continue
            evaluated_count += 1
            decision = candidate.decision
            if not decision.approved:
                blocked_count += 1
                reasons = (
                    *(f"missing:{item}" for item in decision.missing_checks),
                    *(f"violation:{item}" for item in decision.violations),
                )
                for reason in reasons or ("rejected",):
                    blocked[reason] += 1
                if hermes_experimental_can_arm(decision):
                    hermes_candidates.append(candidate)
                else:
                    hard_blocked_count += 1
                    for reason in reasons or ("rejected",):
                        hermes_hard[reason] += 1
                continue
            candidates.append(candidate)

        candidates.sort(key=_candidate_rank)
        hermes_candidates.sort(key=_candidate_rank)
        return V2MarketScanResult(
            markets=tuple(markets),
            candidates=tuple(candidates[:top_n]),
            scanned_count=len(discovery_symbols),
            evaluated_count=evaluated_count,
            blocked_count=blocked_count,
            blocked_reasons=dict(sorted(blocked.items())),
            hermes_candidates=tuple(hermes_candidates[:top_n]),
            hard_blocked_count=hard_blocked_count,
            hermes_hard_blocked_reasons=dict(sorted(hermes_hard.items())),
            errors=errors,
            names=names,
            decision_at=now,
        )


def _candidate_rank(item: DailySetupCandidate) -> tuple[int, int, object, str]:
    return (
        -item.decision.flow_stars,
        -len(item.decision.setups),
        abs(item.decision.ma50_distance),
        item.symbol,
    )


def _candidate_payload(
    item: DailySetupCandidate, *, names: dict[str, str], experimental: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": item.symbol,
        "name": names.get(item.symbol, item.symbol),
        "signalSession": item.signal_session,
        "closePrice": item.close_price,
        "setupLow": item.setup_low,
        "atr14": item.atr14,
        "setups": [setup.value for setup in item.decision.setups],
        "rsi14": item.decision.rsi14,
        "ma50": item.decision.ma50,
        "ma200": item.decision.ma200,
        "ma50Distance": item.decision.ma50_distance,
        "flowStars": item.decision.flow_stars,
        "valuationTier": item.decision.valuation_tier.value,
        "confidenceMultiplier": item.decision.confidence_multiplier,
    }
    if experimental:
        payload["referenceViolations"] = list(item.decision.violations)
    return payload


def v2_market_scan_to_dict(result: V2MarketScanResult) -> dict[str, object]:
    return {
        "entryStrategy": "setup-v2.3-independent-daily",
        "scanScope": "discovery-symbols",
        "decisionAt": result.decision_at,
        "markets": [
            {
                "symbol": item.symbol,
                "name": result.names.get(item.symbol, item.symbol),
                "regime": item.regime.value,
                "asOf": item.as_of,
                "closePrice": item.close_price,
                "momentum20d": item.momentum_20d,
                "volumeRatio": item.volume_ratio,
                "currency": item.currency,
            }
            for item in result.markets
        ],
        "candidateSummary": {
            "scanned": result.scanned_count,
            "evaluated": result.evaluated_count,
            "approved": len(result.candidates),
            "blocked": result.blocked_count,
            "ruleApproved": len(result.candidates),
            "hermesExperimental": len(result.hermes_candidates),
            "hermesEligible": len(result.candidates) + len(result.hermes_candidates),
            "hardBlocked": result.hard_blocked_count,
        },
        "candidates": [
            _candidate_payload(item, names=result.names, experimental=False)
            for item in result.candidates
        ],
        "hermesCandidates": [
            _candidate_payload(item, names=result.names, experimental=True)
            for item in result.hermes_candidates
        ],
        "blockedReasons": result.blocked_reasons,
        "hermesHardBlockedReasons": result.hermes_hard_blocked_reasons,
        "errors": result.errors,
    }
