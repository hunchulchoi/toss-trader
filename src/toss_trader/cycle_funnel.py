from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from json import JSONDecodeError, loads
from typing import Any

from .cycle_state import PaperCycleRun

REVIEW_PURPOSE = (
    "Judge whether today's 1m v2 process followed the rules. "
    "Do not treat returns as skill. Do not invent missed buys from news."
)


def insights_from_runs(runs: Sequence[PaperCycleRun]) -> tuple[dict[str, Any], ...]:
    insights: list[dict[str, Any]] = []
    for run in runs:
        if not run.cycle_insight:
            continue
        try:
            payload = loads(run.cycle_insight)
        except (JSONDecodeError, TypeError, UnicodeError):
            continue
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["_observedAt"] = run.started_at.isoformat()
            insights.append(payload)
    return tuple(insights)


def aggregate_intraday_review(
    insights: Sequence[Mapping[str, Any]],
    *,
    cycle_count: int | None = None,
) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    buy_fills = 0
    sell_fills = 0
    for insight in insights:
        rows = insight.get("symbols")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            state = latest.setdefault(
                symbol,
                {
                    "buyFills": 0,
                    "sellFills": 0,
                    "firstReason": None,
                    "lastReason": None,
                    "lastReasonClass": None,
                    "firstObservedAt": None,
                    "lastObservedAt": None,
                    "reasonCounts": Counter(),
                    "transitionCount": 0,
                    "reasonPath": [],
                    "armRejectDetail": None,
                    "armRejectAt": None,
                    "eventGateShadow": None,
                    "eventGateAt": None,
                },
            )
            fill_side = row.get("fillSide")
            row_error = None
            if fill_side == "BUY":
                buy_fills += 1
                state["buyFills"] += 1
                reason = "filled:BUY"
            elif fill_side == "SELL":
                sell_fills += 1
                state["sellFills"] += 1
                reason = "filled:SELL"
            else:
                row_error = row.get("error")
                reason = row.get("skipReason") or row_error or row.get("reason")
            if reason:
                reason = str(reason)
                reason_class = (
                    "error"
                    if fill_side not in {"BUY", "SELL"} and row_error
                    else _reason_class(reason)
                )
                observed_at = insight.get("_observedAt")
                if state["firstReason"] is None:
                    state["firstReason"] = reason
                    state["firstObservedAt"] = observed_at
                    state["reasonPath"] = [reason]
                elif state["lastReason"] != reason:
                    state["transitionCount"] += 1
                    state["reasonPath"].append(reason)
                state["lastReason"] = reason
                state["lastReasonClass"] = reason_class
                state["lastObservedAt"] = observed_at
                state["reasonCounts"][reason] += 1
                skip_detail = row.get("skipDetail")
                if (
                    state["armRejectDetail"] is None
                    and isinstance(skip_detail, dict)
                    and "below-one-lot" in reason
                ):
                    state["armRejectDetail"] = skip_detail
                    state["armRejectAt"] = observed_at
                if isinstance(skip_detail, dict):
                    event_gate = skip_detail.get("eventGateShadow")
                    if isinstance(event_gate, dict):
                        state["eventGateShadow"] = event_gate
                        state["eventGateAt"] = observed_at

    last_reasons = Counter(
        str(state["lastReason"])
        for state in latest.values()
        if state["lastReason"] is not None
    )
    details = tuple(
        {
            "symbol": symbol,
            "firstReason": state["firstReason"],
            "lastReason": state["lastReason"],
            "firstObservedAt": state["firstObservedAt"],
            "lastObservedAt": state["lastObservedAt"],
            "reasonCounts": dict(state["reasonCounts"]),
            "transitionCount": state["transitionCount"],
            "reasonClass": state["lastReasonClass"]
            or _reason_class(state["lastReason"]),
            "buyFills": state["buyFills"],
            "sellFills": state["sellFills"],
            "reasonPath": list(state["reasonPath"]),
            "armRejectDetail": state["armRejectDetail"],
            "armRejectAt": state["armRejectAt"],
            "eventGateShadow": state["eventGateShadow"],
            "eventGateAt": state["eventGateAt"],
        }
        for symbol, state in sorted(latest.items())
    )
    changed = tuple(
        {
            "symbol": row["symbol"],
            "from": row["firstReason"],
            "to": row["lastReason"],
            "transitions": row["transitionCount"],
        }
        for row in details
        if row["transitionCount"]
    )
    return {
        "schemaVersion": 2,
        "fillScope": "seoul-session-cumulative",
        "purpose": REVIEW_PURPOSE,
        "cycles": len(insights) if cycle_count is None else cycle_count,
        "symbols": len(latest),
        "buyFills": buy_fills,
        "sellFills": sell_fills,
        "lastReasons": dict(last_reasons),
        "reasonClasses": dict(
            Counter(row["reasonClass"] for row in details)
        ),
        "changedFacts": changed,
        "symbolsDetail": details,
    }


def _reason_class(reason: object) -> str:
    value = str(reason or "")
    if value == "error" or "unavailable" in value:
        return "error"
    if ":waiting:" in value:
        return "waiting"
    if ":missing:" in value or "completed-daily-candles" in value:
        return "missing-data"
    return "normal-rejection"


def format_intraday_review_lines(review: Mapping[str, Any]) -> tuple[str, ...]:
    cycles = int(review.get("cycles") or 0)
    if cycles <= 0 and int(review.get("symbols") or 0) <= 0:
        return ("당일 1m 사이클 기록 없음",)
    reasons = review.get("lastReasons")
    reasons = reasons if isinstance(reasons, dict) else {}
    ranked = sorted(
        ((str(reason), int(count)) for reason, count in reasons.items()),
        key=lambda item: (-item[1], item[0]),
    )
    reason_text = (
        ", ".join(f"{reason} {count}" for reason, count in ranked[:5])
        if ranked
        else "없음"
    )
    fills = [
        f"{row['symbol']} {side} {row[key]}"
        for row in review.get("symbolsDetail") or ()
        if isinstance(row, dict)
        for key, side in (("buyFills", "BUY"), ("sellFills", "SELL"))
        if int(row.get(key) or 0)
    ]
    lines = [
        (
            f"당일 1m 사이클 {cycles}, 종목 {int(review.get('symbols') or 0)}, "
            f"paper BUY {int(review.get('buyFills') or 0)} / "
            f"SELL {int(review.get('sellFills') or 0)}"
        ),
        f"마지막 사유: {reason_text}",
    ]
    if fills:
        lines.append("체결 종목: " + ", ".join(fills[:12]))
    return tuple(lines)
