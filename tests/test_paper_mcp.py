import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from toss_trader.paper import PositionAccounting
from toss_trader.paper_mcp import (
    PaperMcpService,
    PostgresPaperReadStore,
    _cycle_status,
    _ledger_status,
    handle_mcp_request,
)


class FakePaperReadStore:
    def status(self) -> dict[str, Any]:
        return {"portfolios": {"rule": {"status": "succeeded"}}}

    def holdings(self) -> dict[str, Any]:
        return {"portfolios": {"hermes": [{"symbol": "005930"}]}}

    def pnl(self) -> dict[str, Any]:
        return {"portfolios": {"rule": {"equity": "1010000"}}}


class PaperMcpServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PaperMcpService(FakePaperReadStore())

    def test_exposes_only_three_paper_read_tools(self) -> None:
        tools = self.service.tools()

        self.assertEqual(
            [tool["name"] for tool in tools],
            ["toss_paper_status", "toss_paper_holdings", "toss_paper_pnl"],
        )
        encoded = str(tools).lower()
        self.assertIn("paper", encoded)
        self.assertNotIn("account", encoded)
        self.assertNotIn("실계좌", encoded)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["properties"], {})
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_calls_fixed_read_methods_and_rejects_arguments(self) -> None:
        self.assertIn("rule", self.service.call("toss_paper_status", {})["portfolios"])
        self.assertIn(
            "hermes", self.service.call("toss_paper_holdings", {})["portfolios"]
        )
        self.assertIn("rule", self.service.call("toss_paper_pnl", {})["portfolios"])
        with self.assertRaisesRegex(ValueError, "does not accept arguments"):
            self.service.call("toss_paper_pnl", {"portfolio": "rule"})
        with self.assertRaisesRegex(ValueError, "unknown MCP tool"):
            self.service.call("holdings", {})

    def test_handles_mcp_initialize_list_and_call(self) -> None:
        initialized = handle_mcp_request(
            self.service,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        )
        assert initialized is not None
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")

        listed = handle_mcp_request(
            self.service,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed is not None
        self.assertEqual(len(listed["result"]["tools"]), 3)

        called = handle_mcp_request(
            self.service,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "toss_paper_pnl", "arguments": {}},
            },
        )
        assert called is not None
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(
            called["result"]["structuredContent"]["portfolios"]["rule"]["equity"],
            "1010000",
        )

    def test_initialized_notification_needs_no_response(self) -> None:
        response = handle_mcp_request(
            self.service,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        self.assertIsNone(response)

    def test_postgres_session_is_forced_read_only(self) -> None:
        captured: dict[str, Any] = {}
        sentinel = object()

        def connect(**kwargs: Any) -> object:
            captured.update(kwargs)
            return sentinel

        store = PostgresPaperReadStore(
            {
                "host": "postgres.internal",
                "port": 5431,
                "user": "reader",
                "password": "secret",
                "dbname": "toss_trader",
            },
            connect=connect,
        )

        self.assertIs(store._open(), sentinel)
        self.assertIn("default_transaction_read_only=on", captured["options"])
        self.assertEqual(captured["application_name"], "toss-paper-mcp")


class CycleStatusPayloadTest(unittest.TestCase):
    def test_cycle_status_exposes_idle_funnel_and_symbol_ma(self) -> None:
        insight = {
            "idleReason": "no-crossover",
            "newBuysAllowed": True,
            "marketRegime": "RISK_ON",
            "funnel": {
                "scanned": 17,
                "evaluated": 17,
                "skippedCandles": 0,
                "noCrossover": 17,
                "sellNoPosition": 0,
                "signals": 0,
                "riskRejected": 0,
                "advisorRejected": 0,
                "fills": 0,
                "failed": 0,
            },
            "reasons": {"no-crossover": 17},
            "symbols": [
                {
                    "symbol": "005930",
                    "reason": "no-crossover",
                    "close": "70000",
                    "maShort": "70100",
                    "maLong": "70500",
                    "relation": "below",
                }
            ],
        }
        started = datetime(2026, 8, 14, 0, 10, tzinfo=UTC)
        payload = _cycle_status(
            (
                "hermes",
                "run-1",
                started,
                started,
                "succeeded",
                "1m",
                17,
                0,
                0,
                0,
                0,
                "0",
                None,
                json.dumps(insight),
            )
        )

        self.assertEqual(payload["idleReason"], "no-crossover")
        self.assertTrue(payload["newBuysAllowed"])
        self.assertEqual(payload["marketRegime"], "RISK_ON")
        self.assertEqual(payload["funnel"]["noCrossover"], 17)
        self.assertEqual(payload["reasons"], {"no-crossover": 17})
        self.assertEqual(payload["symbolStates"][0]["relation"], "below")
        self.assertEqual(payload["signals"], 0)

    def test_ledger_status_adds_cash_weight_and_open_count(self) -> None:
        payload = _ledger_status(
            {
                "initialCash": Decimal(1000000),
                "cash": Decimal(623136),
                "accountings": {
                    "005930": PositionAccounting(
                        symbol="005930",
                        quantity=Decimal(1),
                        cost_basis=Decimal(70000),
                        realized_pnl=Decimal(0),
                        commission=Decimal(0),
                        tax=Decimal(0),
                    )
                },
                "marks": {
                    "005930": {
                        "name": "Samsung",
                        "price": Decimal(376864),
                        "currency": "KRW",
                        "markedAt": "2026-08-14T00:10:00+00:00",
                    }
                },
            }
        )

        self.assertEqual(payload["cash"], "623136")
        self.assertEqual(payload["openPositionCount"], 1)
        self.assertEqual(payload["cashWeight"], str(Decimal("623136") / Decimal("1000000")))


if __name__ == "__main__":
    unittest.main()
