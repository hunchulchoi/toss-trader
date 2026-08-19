from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .models import Side, TradeSignal


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal(300000)
    max_position_notional: Decimal = Decimal(1000000)
    max_daily_buy_count: int = 5
    max_open_positions: int = 10
    daily_loss_limit: Decimal = Decimal("-0.03")
    max_consecutive_api_errors: int = 5
    block_new_buys_before_close: timedelta = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    market_close_at: datetime | None = None
    market_is_business_day: bool = True
    position_notional: Decimal = Decimal(0)
    position_quantity: Decimal = Decimal(0)
    available_cash: Decimal | None = None
    daily_buy_count: int = 0
    open_position_count: int = 0
    daily_return_rate: Decimal = Decimal(0)
    consecutive_api_errors: int = 0
    seen_signal_ids: frozenset[str] = field(default_factory=frozenset)
    new_buys_allowed: bool = True
    advisor_status: str | None = None
    advisor_rationale: str | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UniverseCandidateRisk:
    symbol: str
    reference_price: Decimal
    security_type: str
    is_common_share: bool
    status: str
    trading_suspended: bool


@dataclass(frozen=True, slots=True)
class UniverseRiskContext:
    quantity: Decimal
    available_cash: Decimal
    daily_return_rate: Decimal
    consecutive_api_errors: int


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def evaluate(self, signal: TradeSignal, context: RiskContext) -> RiskDecision:
        violations: list[str] = []

        if signal.signal_id in context.seen_signal_ids:
            violations.append("duplicate-signal")
        if signal.side is Side.BUY and not context.new_buys_allowed:
            violations.append("universe-refresh-failed")
        if context.advisor_status == "rejected":
            violations.append(
                f"Hermes 거부: {context.advisor_rationale or '구체적 근거 없음'}"
            )
        elif context.advisor_status == "unavailable":
            violations.append("Hermes 분석 실패: 응답을 받지 못해 체결 차단")
        if signal.notional > self._limits.max_order_notional:
            violations.append("max-order-notional")
        if (
            signal.side is Side.BUY
            and context.available_cash is not None
            and signal.notional > context.available_cash
        ):
            violations.append("insufficient-paper-cash")
        if (
            signal.side is Side.BUY
            and context.position_notional + signal.notional
            > self._limits.max_position_notional
        ):
            violations.append("max-position-notional")
        if signal.side is Side.SELL and signal.quantity > context.position_quantity:
            violations.append("insufficient-position")
        if (
            signal.side is Side.BUY
            and context.daily_buy_count >= self._limits.max_daily_buy_count
        ):
            violations.append("max-daily-buys")
        if (
            signal.side is Side.BUY
            and context.position_quantity <= 0
            and context.open_position_count >= self._limits.max_open_positions
        ):
            violations.append("max-open-positions")
        if (
            signal.side is Side.BUY
            and context.daily_return_rate <= self._limits.daily_loss_limit
        ):
            violations.append("daily-loss-limit")
        if context.consecutive_api_errors >= self._limits.max_consecutive_api_errors:
            violations.append("api-error-kill-switch")
        if not context.market_is_business_day:
            violations.append("market-closed")
        if (
            signal.side is Side.BUY
            and context.market_close_at is not None
            and context.now
            >= context.market_close_at - self._limits.block_new_buys_before_close
        ):
            violations.append("market-close-window")

        return RiskDecision(approved=not violations, violations=tuple(violations))

    def evaluate_universe_candidate(
        self, candidate: UniverseCandidateRisk, context: UniverseRiskContext
    ) -> RiskDecision:
        del context
        violations: list[str] = []
        if candidate.security_type != "STOCK":
            violations.append("unsupported-security-type")
        if not candidate.is_common_share:
            violations.append("not-common-share")
        if candidate.status != "ACTIVE":
            violations.append("stock-not-active")
        if candidate.trading_suspended:
            violations.append("trading-suspended")
        if candidate.reference_price <= 0:
            violations.append("invalid-reference-price")
        return RiskDecision(approved=not violations, violations=tuple(violations))


class N8nRiskManager:
    def __init__(
        self,
        *,
        webhook_url: str,
        token: str,
        limits: RiskLimits | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        if not webhook_url.startswith(("http://", "https://")):
            raise ValueError("RiskManager webhook URL must use HTTP(S)")
        if len(token) < 16:
            raise ValueError("RiskManager webhook token is missing or too short")
        self._webhook_url = webhook_url
        self._token = token
        self._limits = limits or RiskLimits()
        self._timeout_seconds = timeout_seconds

    def evaluate(self, signal: TradeSignal, context: RiskContext) -> RiskDecision:
        return self._call(
            {
                "kind": "trade",
                "signal": _trade_signal_payload(signal),
                "context": _risk_context_payload(context),
            },
            policy_version=1,
        )

    def evaluate_universe_candidate(
        self, candidate: UniverseCandidateRisk, context: UniverseRiskContext
    ) -> RiskDecision:
        return self._call(
            {
                "kind": "universe",
                "candidate": {
                    "symbol": candidate.symbol,
                    "referencePrice": str(candidate.reference_price),
                    "securityType": candidate.security_type,
                    "isCommonShare": candidate.is_common_share,
                    "status": candidate.status,
                    "tradingSuspended": candidate.trading_suspended,
                },
                "context": {
                    "quantity": str(context.quantity),
                    "availableCash": str(context.available_cash),
                    "dailyReturnRate": str(context.daily_return_rate),
                    "consecutiveApiErrors": context.consecutive_api_errors,
                },
            },
            policy_version=2,
        )

    def _call(
        self, payload: dict[str, object], *, policy_version: int
    ) -> RiskDecision:
        payload["limits"] = _risk_limits_payload(
            self._limits, policy_version=policy_version
        )
        payload["parent"] = {
            "workflowId": os.environ.get("N8N_PARENT_WORKFLOW_ID", "unknown"),
            "executionId": os.environ.get("N8N_PARENT_EXECUTION_ID", "unknown"),
            "portfolioId": os.environ.get("PAPER_PORTFOLIO_ID", "unknown"),
            "interval": os.environ.get("PAPER_INTERVAL", "unknown"),
        }
        request = urllib.request.Request(
            self._webhook_url,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                result = json.load(response)
            return _remote_decision(result)
        except (
            OSError,
            TypeError,
            ValueError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ):
            return RiskDecision(
                approved=False,
                violations=("risk-manager-workflow-unavailable",),
            )


def _risk_limits_payload(
    limits: RiskLimits, *, policy_version: int
) -> dict[str, object]:
    return {
        "policyVersion": policy_version,
        "maxOrderNotional": str(limits.max_order_notional),
        "maxPositionNotional": str(limits.max_position_notional),
        "maxDailyBuyCount": limits.max_daily_buy_count,
        "maxOpenPositions": limits.max_open_positions,
        "dailyLossLimit": str(limits.daily_loss_limit),
        "maxConsecutiveApiErrors": limits.max_consecutive_api_errors,
        "blockNewBuysBeforeCloseSeconds": int(
            limits.block_new_buys_before_close.total_seconds()
        ),
    }


def _trade_signal_payload(signal: TradeSignal) -> dict[str, object]:
    return {
        "signalId": signal.signal_id,
        "symbol": signal.symbol,
        "side": signal.side.value,
        "referencePrice": str(signal.reference_price),
        "quantity": str(signal.quantity),
        "reason": signal.reason,
    }


def _risk_context_payload(context: RiskContext) -> dict[str, object]:
    return {
        "now": context.now.isoformat(),
        "marketCloseAt": (
            context.market_close_at.isoformat()
            if context.market_close_at is not None
            else None
        ),
        "marketIsBusinessDay": context.market_is_business_day,
        "positionNotional": str(context.position_notional),
        "positionQuantity": str(context.position_quantity),
        "availableCash": (
            str(context.available_cash) if context.available_cash is not None else None
        ),
        "dailyBuyCount": context.daily_buy_count,
        "openPositionCount": context.open_position_count,
        "dailyReturnRate": str(context.daily_return_rate),
        "consecutiveApiErrors": context.consecutive_api_errors,
        "seenSignalIds": sorted(context.seen_signal_ids),
        "newBuysAllowed": context.new_buys_allowed,
        "advisorStatus": context.advisor_status,
        "advisorRationale": context.advisor_rationale,
    }


def _remote_decision(payload: object) -> RiskDecision:
    if not isinstance(payload, dict) or not isinstance(payload.get("approved"), bool):
        raise TypeError("RiskManager workflow returned invalid JSON")
    violations = payload.get("violations")
    if not isinstance(violations, list) or not all(
        isinstance(value, str) for value in violations
    ):
        raise TypeError("RiskManager workflow returned invalid violations")
    return RiskDecision(
        approved=payload["approved"],
        violations=tuple(violations),
    )
