from __future__ import annotations

import json
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from toss_trader.advisor import parse_hermes_trade_advice
from toss_trader.execution import HERMES_HUNTER_SIGNAL_REASON

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "2026-08-26_morning_reclaim_lost.json"
)


def _advice_from_trade(trade: dict[str, object], *, signal_reason: str):
    content = json.dumps(
        {
            "approved": trade["approved"],
            "rationale": trade["rationale"],
            "vetoCodes": trade["vetoCodes"],
            "evidence": trade.get("evidence") or {},
        },
        ensure_ascii=False,
    )
    return parse_hermes_trade_advice(content, signal_reason=signal_reason)


def _paired_reviews(
    fixture: dict[str, object],
) -> list[tuple[dict[str, object], dict[str, object]]]:
    trades = list(fixture["hermesTrades"])  # type: ignore[arg-type]
    decisions = list(fixture["riskDecisions"])  # type: ignore[arg-type]
    by_symbol_decisions: dict[str, list[dict[str, object]]] = {}
    for row in decisions:
        by_symbol_decisions.setdefault(str(row["symbol"]), []).append(row)
    used: dict[str, int] = {}
    paired: list[tuple[dict[str, object], dict[str, object]]] = []
    for trade in trades:
        symbol = str(trade["symbol"])
        index = used.get(symbol, 0)
        rows = by_symbol_decisions[symbol]
        paired.append((trade, rows[index]))
        used[symbol] = index + 1
    return paired


def _first_counterfactual_entries(
    fixture: dict[str, object],
) -> dict[str, dict[str, object]]:
    held: dict[str, dict[str, object]] = {}
    for trade, decision in _paired_reviews(fixture):
        reason = str(decision["signalReason"])
        advice, ignored = _advice_from_trade(trade, signal_reason=reason)
        if not advice.approved or str(trade["symbol"]) in held:
            continue
        held[str(trade["symbol"])] = {
            "symbol": trade["symbol"],
            "evaluatedAt": decision["evaluatedAt"],
            "quantity": Decimal(str(decision["quantity"])),
            "price": Decimal(str(decision["referencePrice"])),
            "ignoredVetoCodes": ignored,
            "signalReason": reason,
        }
    return held


def _bar_close_and_min_low(
    bars: list[list[str]], *, from_event: datetime
) -> tuple[Decimal, Decimal]:
    start = from_event.replace(second=0, microsecond=0)
    closes: list[Decimal] = []
    lows: list[Decimal] = []
    for row in bars:
        stamp = datetime.fromisoformat(row[0])
        if stamp < start:
            continue
        lows.append(Decimal(row[3]))
        closes.append(Decimal(row[4]))
    if not closes:
        raise AssertionError(f"no bars at or after {start.isoformat()}")
    return closes[-1], min(lows)


class MorningReclaimLostReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())

    def test_stored_morning_reclaim_vetoes_were_daily_setup_not_hunter(self) -> None:
        reasons = {row["signalReason"] for row in self.fixture["riskDecisions"]}
        self.assertTrue(reasons)
        self.assertNotIn(HERMES_HUNTER_SIGNAL_REASON, reasons)
        reclaim_only = [
            trade
            for trade in self.fixture["hermesTrades"]
            if trade["vetoCodes"] == ["RECLAIM_LOST"]
        ]
        self.assertGreaterEqual(len(reclaim_only), 4)

    def test_new_scope_would_arm_all_four_names_at_first_0905_reject(self) -> None:
        entries = _first_counterfactual_entries(self.fixture)
        self.assertEqual(set(entries), {"090360", "079650", "067170", "126640"})
        for entry in entries.values():
            self.assertLessEqual(
                datetime.fromisoformat(str(entry["evaluatedAt"])),
                datetime.fromisoformat("2026-08-26T09:06:59+09:00"),
            )
            self.assertEqual(entry["ignoredVetoCodes"], ("RECLAIM_LOST",))
            self.assertFalse(str(entry["signalReason"]).startswith("hermes Hunter"))

    def test_seosan_delay_was_chase_robostar_delay_was_cheaper(self) -> None:
        entries = _first_counterfactual_entries(self.fixture)
        live = {row["symbol"]: Decimal(row["price"]) for row in self.fixture["fills"]}
        self.assertLess(entries["079650"]["price"], live["079650"])  # type: ignore[operator]
        self.assertGreater(entries["090360"]["price"], live["090360"])  # type: ignore[operator]

    def test_noon_mark_seosan_less_hurt_if_0905_fill_robostar_worse(self) -> None:
        entries = _first_counterfactual_entries(self.fixture)
        live = {
            row["symbol"]: {
                "price": Decimal(row["price"]),
                "quantity": Decimal(row["quantity"]),
                "at": datetime.fromisoformat(row["executedAt"]),
            }
            for row in self.fixture["fills"]
        }
        seosan_mark, seosan_low = _bar_close_and_min_low(
            self.fixture["minutes"]["079650"],
            from_event=datetime.fromisoformat(str(entries["079650"]["evaluatedAt"])),
        )
        robo_mark, robo_low = _bar_close_and_min_low(
            self.fixture["minutes"]["090360"],
            from_event=datetime.fromisoformat(str(entries["090360"]["evaluatedAt"])),
        )
        cf_seosan = (seosan_mark - entries["079650"]["price"]) * entries["079650"][  # type: ignore[operator]
            "quantity"
        ]
        live_seosan = (seosan_mark - live["079650"]["price"]) * live["079650"]["quantity"]
        cf_robo = (robo_mark - entries["090360"]["price"]) * entries["090360"][  # type: ignore[operator]
            "quantity"
        ]
        live_robo = (robo_mark - live["090360"]["price"]) * live["090360"]["quantity"]
        self.assertGreater(cf_seosan, live_seosan)
        self.assertLess(cf_robo, live_robo)
        self.assertLess(seosan_low, entries["079650"]["price"])
        self.assertLess(robo_low, entries["090360"]["price"])


if __name__ == "__main__":
    unittest.main()
