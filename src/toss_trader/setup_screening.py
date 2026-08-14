from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from itertools import pairwise

from .models import Candle, Side, TradeSignal
from .paper import toss_trade_costs


class SetupType(StrEnum):
    PULLBACK = "pullback"
    OVERSOLD_REVERSAL = "oversold-reversal"
    FLOW_REVERSAL = "flow-reversal"


class ValuationTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"

    @property
    def proposed_multiplier(self) -> Decimal:
        return {
            ValuationTier.A: Decimal("1.5"),
            ValuationTier.B: Decimal("1.0"),
            ValuationTier.C: Decimal("0.6"),
        }[self]


@dataclass(frozen=True, slots=True)
class FlowObservation:
    symbol: str
    session_index: int
    session_date: date
    available_at: datetime
    foreign_net_buy: Decimal
    institutional_net_buy: Decimal
    trading_value: Decimal


@dataclass(frozen=True, slots=True)
class FlowSummary:
    latest_session: date
    previous_5d_ratio: Decimal
    current_5d_ratio: Decimal
    institutional_5d_ratio: Decimal
    foreign_reversal: bool
    institutional_confirmed: bool


@dataclass(frozen=True, slots=True)
class ValuationEvidence:
    forward_eps_growth: Decimal | None = None
    sector_per_percentile: Decimal | None = None
    sector_relative_overvalued: bool = False
    pure_technical_rebound: bool = False


@dataclass(frozen=True, slots=True)
class SetupContext:
    decision_at: datetime
    signal_session: date
    flow_observations: tuple[FlowObservation, ...] = ()
    valuation: ValuationEvidence | None = None
    stop_price: Decimal | None = None
    averaging_down: bool = False
    event_imminent: bool | None = None
    gap_up_chase: bool | None = None


@dataclass(frozen=True, slots=True)
class SetupDecision:
    symbol: str
    approved: bool
    setups: tuple[SetupType, ...]
    violations: tuple[str, ...]
    missing_checks: tuple[str, ...]
    rsi14: Decimal
    ma50: Decimal
    ma200: Decimal
    ma50_distance: Decimal
    flow_stars: int
    flow_summary: FlowSummary | None
    valuation_tier: ValuationTier
    confidence_multiplier: Decimal
    proposed_confidence_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class PositionSizingPolicy:
    per_trade_risk_rate: Decimal = Decimal("0.005")
    max_open_heat_rate: Decimal = Decimal("0.02")
    max_cluster_heat_rate: Decimal = Decimal("0.01")
    max_order_notional: Decimal = Decimal(300000)
    atr_stop_multiple: Decimal = Decimal("1.5")
    lot_size: Decimal = Decimal(1)


@dataclass(frozen=True, slots=True)
class SlippageAssumption:
    entry_rate: Decimal = Decimal(0)
    exit_rate: Decimal = Decimal(0)


DEFAULT_POSITION_SIZING_POLICY = PositionSizingPolicy()
NO_SLIPPAGE = SlippageAssumption()


@dataclass(frozen=True, slots=True)
class PositionSizeReference:
    approved: bool
    quantity: Decimal
    executable_notional: Decimal
    per_trade_budget: Decimal
    remaining_open_heat: Decimal
    remaining_cluster_heat: Decimal
    usable_risk_budget: Decimal
    structural_stop_distance: Decimal
    atr_stop_floor: Decimal
    effective_stop_distance: Decimal
    estimated_loss: Decimal
    planned_heat: Decimal
    required_cash: Decimal
    limiting_factors: tuple[str, ...]


def summarize_flow(
    observations: tuple[FlowObservation, ...],
    *,
    symbol: str,
    signal_session: date,
    decision_at: datetime,
) -> FlowSummary | None:
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("decision_at must include a timezone offset")
    if any(item.symbol != symbol for item in observations):
        raise ValueError("flow observations must match symbol")
    if any(
        item.available_at.tzinfo is None or item.available_at.utcoffset() is None
        for item in observations
    ):
        raise ValueError("flow available_at must include a timezone offset")
    if any(item.trading_value <= 0 for item in observations):
        raise ValueError("flow trading_value must be positive")
    if any(
        previous.session_date >= current.session_date
        or current.session_index != previous.session_index + 1
        for previous, current in pairwise(observations)
    ):
        raise ValueError("flow sessions must be consecutive and strictly increasing")

    eligible = tuple(
        item
        for item in observations
        if item.session_date <= signal_session and item.available_at <= decision_at
    )
    if len(eligible) < 6:
        return None
    window = eligible[-6:]
    previous = window[:5]
    current = window[1:]
    previous_ratio = _flow_ratio(previous, "foreign_net_buy")
    current_ratio = _flow_ratio(current, "foreign_net_buy")
    institutional_ratio = _flow_ratio(current, "institutional_net_buy")
    latest = window[-1]
    return FlowSummary(
        latest_session=latest.session_date,
        previous_5d_ratio=_rounded(previous_ratio),
        current_5d_ratio=_rounded(current_ratio),
        institutional_5d_ratio=_rounded(institutional_ratio),
        foreign_reversal=(
            previous_ratio < 0 < current_ratio and latest.foreign_net_buy > 0
        ),
        institutional_confirmed=(
            institutional_ratio > 0 and latest.institutional_net_buy > 0
        ),
    )


def evaluate_setup(
    candles: list[Candle], *, context: SetupContext
) -> SetupDecision:
    _validate_candles(candles)
    if context.decision_at.tzinfo is None or context.decision_at.utcoffset() is None:
        raise ValueError("decision_at must include a timezone offset")
    latest = candles[-200:]
    closes = [item.close_price for item in latest]
    current = closes[-1]
    previous = latest[-2]
    candle = latest[-1]
    ma50 = _average(closes[-50:])
    ma200 = _average(closes)
    distance = current / ma50 - Decimal(1)
    rsi14 = _rsi(closes, 14)

    setups: list[SetupType] = []
    if current > ma50 > ma200 and Decimal(0) <= distance <= Decimal("0.04"):
        setups.append(SetupType.PULLBACK)
    if (
        rsi14 <= 35
        and candle.close_price > candle.open_price
        and current > previous.high_price
    ):
        setups.append(SetupType.OVERSOLD_REVERSAL)

    flow = summarize_flow(
        context.flow_observations,
        symbol=candle.symbol,
        signal_session=context.signal_session,
        decision_at=context.decision_at,
    )
    flow_stars = 0
    if flow is not None and flow.foreign_reversal:
        setups.append(SetupType.FLOW_REVERSAL)
        flow_stars = 2 if flow.institutional_confirmed else 1

    violations: list[str] = []
    if not any(
        setup in {SetupType.PULLBACK, SetupType.OVERSOLD_REVERSAL}
        for setup in setups
    ):
        violations.append("missing-price-setup")
    if flow is not None and not flow.foreign_reversal:
        violations.append("flow-not-confirmed")
    if rsi14 >= 70:
        violations.append("rsi-chase")
    if current / previous.close_price - Decimal(1) <= Decimal("-0.03"):
        violations.append("falling-knife")
    if (
        context.stop_price is not None
        and current <= context.stop_price * Decimal("1.05")
    ):
        violations.append("stop-line-proximity")
    if context.averaging_down:
        violations.append("averaging-down")
    if context.event_imminent is True:
        violations.append("event-imminent")
    if context.gap_up_chase is True:
        violations.append("gap-up-chase")

    missing_checks: list[str] = []
    if flow is None:
        missing_checks.append("flow-history")
    if context.event_imminent is None:
        missing_checks.append("event-calendar")
    if context.gap_up_chase is None:
        missing_checks.append("gap-up-review")

    tier = valuation_tier(context.valuation)
    return SetupDecision(
        symbol=candle.symbol,
        approved=not violations and not missing_checks,
        setups=tuple(setups),
        violations=tuple(violations),
        missing_checks=tuple(missing_checks),
        rsi14=_rounded(rsi14),
        ma50=_rounded(ma50),
        ma200=_rounded(ma200),
        ma50_distance=_rounded(distance),
        flow_stars=flow_stars,
        flow_summary=flow,
        valuation_tier=tier,
        confidence_multiplier=Decimal("1.0"),
        proposed_confidence_multiplier=tier.proposed_multiplier,
    )


def valuation_tier(evidence: ValuationEvidence | None) -> ValuationTier:
    if evidence is None:
        return ValuationTier.B
    if evidence.sector_relative_overvalued or evidence.pure_technical_rebound:
        return ValuationTier.C
    if (
        evidence.forward_eps_growth is not None
        and evidence.forward_eps_growth >= Decimal("0.25")
        and evidence.sector_per_percentile is not None
        and evidence.sector_per_percentile <= Decimal("0.30")
    ):
        return ValuationTier.A
    return ValuationTier.B


def position_size_reference(
    *,
    symbol: str,
    equity: Decimal,
    reference_price: Decimal,
    stop_price: Decimal,
    atr: Decimal,
    available_cash: Decimal,
    current_open_heat: Decimal,
    current_cluster_heat: Decimal,
    policy: PositionSizingPolicy = DEFAULT_POSITION_SIZING_POLICY,
    slippage: SlippageAssumption = NO_SLIPPAGE,
) -> PositionSizeReference:
    _validate_sizing_inputs(
        equity=equity,
        reference_price=reference_price,
        stop_price=stop_price,
        atr=atr,
        available_cash=available_cash,
        current_open_heat=current_open_heat,
        current_cluster_heat=current_cluster_heat,
        policy=policy,
        slippage=slippage,
    )
    per_trade_budget = equity * policy.per_trade_risk_rate
    remaining_open = max(
        Decimal(0), equity * policy.max_open_heat_rate - current_open_heat
    )
    remaining_cluster = max(
        Decimal(0), equity * policy.max_cluster_heat_rate - current_cluster_heat
    )
    usable = min(per_trade_budget, remaining_open, remaining_cluster)
    structural_distance = reference_price - stop_price
    atr_floor = atr * policy.atr_stop_multiple
    effective_distance = max(structural_distance, atr_floor)
    exit_reference = reference_price - effective_distance
    if exit_reference <= 0:
        raise ValueError("effective stop distance must be below reference_price")

    entry_price = reference_price * (Decimal(1) + slippage.entry_rate)
    exit_price = exit_reference * (Decimal(1) - slippage.exit_rate)
    base_loss = entry_price - exit_price
    risk_quantity = _floor_lot(usable / base_loss, policy.lot_size)
    order_quantity = _floor_lot(
        policy.max_order_notional / entry_price, policy.lot_size
    )
    cash_quantity = _floor_lot(available_cash / entry_price, policy.lot_size)
    quantity = min(risk_quantity, order_quantity, cash_quantity)
    reduced_for_risk_costs = False
    reduced_for_cash_costs = False

    while quantity > 0:
        planned_heat, required_cash = _round_trip_risk(
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
        )
        if planned_heat <= usable and required_cash <= available_cash:
            break
        reduced_for_risk_costs |= planned_heat > usable
        reduced_for_cash_costs |= required_cash > available_cash
        quantity -= policy.lot_size
    if quantity <= 0:
        quantity = Decimal(0)
        planned_heat = Decimal(0)
        required_cash = Decimal(0)
    else:
        planned_heat, required_cash = _round_trip_risk(
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
        )

    factors = _limiting_factors(
        quantity=quantity,
        risk_quantity=risk_quantity,
        order_quantity=order_quantity,
        cash_quantity=cash_quantity,
        per_trade_budget=per_trade_budget,
        remaining_open=remaining_open,
        remaining_cluster=remaining_cluster,
        usable=usable,
        reduced_for_risk_costs=reduced_for_risk_costs,
        reduced_for_cash_costs=reduced_for_cash_costs,
    )
    return PositionSizeReference(
        approved=quantity > 0,
        quantity=quantity,
        executable_notional=quantity * reference_price,
        per_trade_budget=per_trade_budget,
        remaining_open_heat=remaining_open,
        remaining_cluster_heat=remaining_cluster,
        usable_risk_budget=usable,
        structural_stop_distance=structural_distance,
        atr_stop_floor=atr_floor,
        effective_stop_distance=effective_distance,
        estimated_loss=planned_heat,
        planned_heat=planned_heat,
        required_cash=required_cash,
        limiting_factors=factors,
    )


def _round_trip_risk(
    *, symbol: str, quantity: Decimal, entry_price: Decimal, exit_price: Decimal
) -> tuple[Decimal, Decimal]:
    buy = TradeSignal(
        signal_id="setup-size-buy",
        symbol=symbol,
        side=Side.BUY,
        reference_price=entry_price,
        quantity=quantity,
        reason="setup sizing",
    )
    sell = TradeSignal(
        signal_id="setup-size-sell",
        symbol=symbol,
        side=Side.SELL,
        reference_price=exit_price,
        quantity=quantity,
        reason="setup sizing",
    )
    buy_costs = toss_trade_costs(buy)
    sell_costs = toss_trade_costs(sell)
    principal_loss = (entry_price - exit_price) * quantity
    return (
        principal_loss + buy_costs.total + sell_costs.total,
        buy.notional + buy_costs.total,
    )


def _limiting_factors(
    *,
    quantity: Decimal,
    risk_quantity: Decimal,
    order_quantity: Decimal,
    cash_quantity: Decimal,
    per_trade_budget: Decimal,
    remaining_open: Decimal,
    remaining_cluster: Decimal,
    usable: Decimal,
    reduced_for_risk_costs: bool,
    reduced_for_cash_costs: bool,
) -> tuple[str, ...]:
    factors: list[str] = []
    if usable == per_trade_budget:
        factors.append("per-trade-risk")
    if usable == remaining_open:
        factors.append("open-heat")
    if usable == remaining_cluster:
        factors.append("cluster-heat")
    if quantity == order_quantity:
        factors.append("max-order-notional")
    if quantity == cash_quantity or reduced_for_cash_costs:
        factors.append("available-cash")
    if reduced_for_risk_costs:
        factors.append("round-trip-costs")
    if quantity == 0:
        factors.append("below-one-lot")
    return tuple(dict.fromkeys(factors))


def _validate_sizing_inputs(
    *,
    equity: Decimal,
    reference_price: Decimal,
    stop_price: Decimal,
    atr: Decimal,
    available_cash: Decimal,
    current_open_heat: Decimal,
    current_cluster_heat: Decimal,
    policy: PositionSizingPolicy,
    slippage: SlippageAssumption,
) -> None:
    if equity <= 0:
        raise ValueError("equity must be positive")
    if not Decimal(0) < stop_price < reference_price:
        raise ValueError("stop_price must satisfy 0 < stop < reference_price")
    if atr <= 0:
        raise ValueError("atr must be positive")
    if min(available_cash, current_open_heat, current_cluster_heat) < 0:
        raise ValueError("cash and heat values must not be negative")
    rates = (
        policy.per_trade_risk_rate,
        policy.max_open_heat_rate,
        policy.max_cluster_heat_rate,
    )
    if any(not Decimal(0) < rate <= 1 for rate in rates):
        raise ValueError("risk rates must satisfy 0 < rate <= 1")
    if min(
        reference_price,
        policy.max_order_notional,
        policy.atr_stop_multiple,
        policy.lot_size,
    ) <= 0:
        raise ValueError("price and sizing policy values must be positive")
    if any(not Decimal(0) <= rate < 1 for rate in (slippage.entry_rate, slippage.exit_rate)):
        raise ValueError("slippage rates must satisfy 0 <= rate < 1")


def _floor_lot(value: Decimal, lot_size: Decimal) -> Decimal:
    return (value / lot_size).to_integral_value(rounding=ROUND_FLOOR) * lot_size


def _flow_ratio(
    observations: tuple[FlowObservation, ...], field: str
) -> Decimal:
    numerator = sum(
        (getattr(item, field) for item in observations), start=Decimal(0)
    )
    denominator = sum(
        (item.trading_value for item in observations), start=Decimal(0)
    )
    return numerator / denominator


def _validate_candles(candles: list[Candle]) -> None:
    if len(candles) < 200:
        raise ValueError(f"need 200 daily candles, found {len(candles)}")
    latest = candles[-200:]
    if any(item.interval != "1d" for item in latest):
        raise ValueError("setup screening requires daily candles")
    if len({item.symbol for item in latest}) != 1:
        raise ValueError("setup screening candles must share one symbol")


def _rsi(closes: list[Decimal], period: int) -> Decimal:
    changes = [current - previous for previous, current in pairwise(closes)]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    average_gain = _average(gains[:period])
    average_loss = _average(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_gain == 0 and average_loss == 0:
        return Decimal(50)
    if average_loss == 0:
        return Decimal(100)
    relative_strength = average_gain / average_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, start=Decimal(0)) / Decimal(len(values))


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))
