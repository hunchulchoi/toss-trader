import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from toss_trader.paper import PositionAccounting
from toss_trader.paper_mcp import (
    PANEL_MCP_TOOLS,
    PUBLIC_MCP_TOOLS,
    PaperMcpService,
    PostgresPaperReadStore,
    _cycle_status,
    _ledger_status,
    _minute_evidence,
    _panel_cutoff,
    _panel_evidence_arguments,
    _symbol_reason_traces,
    handle_mcp_request,
)


class FakePaperReadStore:
    def status(self) -> dict[str, Any]:
        return {"portfolios": {"rule": {"status": "succeeded"}}}

    def holdings(self) -> dict[str, Any]:
        return {"portfolios": {"hermes": [{"symbol": "005930"}]}}

    def pnl(self) -> dict[str, Any]:
        return {"portfolios": {"rule": {"equity": "1010000"}}}

    def panel_evidence(
        self, panel_id: str, topic: str, symbols: tuple[str, ...]
    ) -> dict[str, Any]:
        return {"panelId": panel_id, "topic": topic, "symbols": list(symbols)}


class PaperMcpServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PaperMcpService(FakePaperReadStore())

    def test_exposes_four_fixed_paper_read_tools(self) -> None:
        tools = self.service.tools()

        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "toss_paper_status",
                "toss_paper_holdings",
                "toss_paper_pnl",
                "toss_paper_panel_evidence",
            ],
        )
        encoded = str(tools).lower()
        self.assertIn("paper", encoded)
        self.assertNotIn("account", encoded)
        self.assertNotIn("실계좌", encoded)
        for tool in tools[:3]:
            self.assertEqual(tool["inputSchema"]["properties"], {})
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        evidence_schema = tools[3]["inputSchema"]
        self.assertEqual(evidence_schema["required"], ["panelId", "topic"])
        self.assertEqual(
            evidence_schema["properties"]["topic"]["enum"],
            ["session-summary", "symbol-trace"],
        )
        self.assertEqual(evidence_schema["properties"]["symbols"]["maxItems"], 10)
        self.assertFalse(evidence_schema["additionalProperties"])

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

    def test_calls_cutoff_panel_evidence_with_validated_arguments(self) -> None:
        panel_id = "11111111-1111-4111-8111-111111111111"

        summary = self.service.call(
            "toss_paper_panel_evidence",
            {"panelId": panel_id, "topic": "session-summary"},
        )
        trace = self.service.call(
            "toss_paper_panel_evidence",
            {
                "panelId": panel_id,
                "topic": "symbol-trace",
                "symbols": ["005930", "005930", " 000660 "],
            },
        )

        self.assertEqual(summary["symbols"], [])
        self.assertEqual(trace["symbols"], ["005930", "000660"])

    def test_rejects_broad_or_malformed_panel_evidence_searches(self) -> None:
        panel_id = "11111111-1111-4111-8111-111111111111"
        invalid = (
            None,
            {"panelId": "not-a-uuid", "topic": "session-summary"},
            {"panelId": panel_id, "topic": "arbitrary-sql"},
            {"panelId": panel_id, "topic": "symbol-trace"},
            {
                "panelId": panel_id,
                "topic": "symbol-trace",
                "symbols": [f"{value:06d}" for value in range(11)],
            },
            {
                "panelId": panel_id,
                "topic": "symbol-trace",
                "symbols": ["005930;DROP"],
            },
            {
                "panelId": panel_id,
                "topic": "session-summary",
                "symbols": ["005930"],
            },
            {"panelId": panel_id, "topic": "session-summary", "sql": "SELECT 1"},
        )

        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.service.call("toss_paper_panel_evidence", arguments)

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
        self.assertEqual(len(listed["result"]["tools"]), 4)

        public_listed = handle_mcp_request(
            self.service,
            {"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
            allowed_tools=PUBLIC_MCP_TOOLS,
        )
        panel_listed = handle_mcp_request(
            self.service,
            {"jsonrpc": "2.0", "id": 22, "method": "tools/list", "params": {}},
            allowed_tools=PANEL_MCP_TOOLS,
        )
        assert public_listed is not None
        assert panel_listed is not None
        self.assertEqual(len(public_listed["result"]["tools"]), 3)
        self.assertEqual(
            [tool["name"] for tool in panel_listed["result"]["tools"]],
            ["toss_paper_panel_evidence"],
        )

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

        blocked = handle_mcp_request(
            self.service,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "toss_paper_status", "arguments": {}},
            },
            allowed_tools=PANEL_MCP_TOOLS,
        )
        assert blocked is not None
        self.assertTrue(blocked["result"]["isError"])
        self.assertIn("does not expose", blocked["result"]["content"][0]["text"])

        panel_blocked_on_public = handle_mcp_request(
            self.service,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "toss_paper_panel_evidence",
                    "arguments": {
                        "panelId": "11111111-1111-4111-8111-111111111111",
                        "topic": "session-summary",
                    },
                },
            },
            allowed_tools=PUBLIC_MCP_TOOLS,
        )
        assert panel_blocked_on_public is not None
        self.assertTrue(panel_blocked_on_public["result"]["isError"])

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


class PanelEvidencePayloadTest(unittest.TestCase):
    class _PanelCursor:
        def __init__(self, context: dict[str, Any]) -> None:
            self.context = context
            self.parameters: tuple[Any, ...] | None = None

        def __enter__(self) -> "PanelEvidencePayloadTest._PanelCursor":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, _: str, parameters: tuple[Any, ...]) -> None:
            self.parameters = parameters

        def fetchone(self) -> tuple[dict[str, Any]]:
            return (self.context,)

    class _PanelConnection:
        def __init__(self, context: dict[str, Any]) -> None:
            self.cursor_instance = PanelEvidencePayloadTest._PanelCursor(context)

        def cursor(self) -> "PanelEvidencePayloadTest._PanelCursor":
            return self.cursor_instance

    def test_argument_parser_normalizes_and_deduplicates_symbols(self) -> None:
        panel_id, topic, symbols = _panel_evidence_arguments(
            {
                "panelId": "11111111-1111-4111-8111-111111111111",
                "topic": "symbol-trace",
                "symbols": ["005930", " 000660", "005930"],
            }
        )

        self.assertEqual(panel_id, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(topic, "symbol-trace")
        self.assertEqual(symbols, ("005930", "000660"))

    def test_panel_cutoff_comes_from_stored_observed_at(self) -> None:
        observed_at = datetime.now(UTC) - timedelta(minutes=1)
        connection = self._PanelConnection(
            {
                "briefing": {
                    "kind": "midday",
                    "observedAt": observed_at.isoformat(),
                }
            }
        )

        cutoff = _panel_cutoff(
            connection, "11111111-1111-4111-8111-111111111111"
        )

        self.assertEqual(cutoff["observedAt"], observed_at)
        self.assertEqual(cutoff["briefingKind"], "midday")
        self.assertEqual(
            cutoff["businessDate"],
            observed_at.astimezone(ZoneInfo("Asia/Seoul")).date(),
        )
        self.assertEqual(
            connection.cursor_instance.parameters,
            ("11111111-1111-4111-8111-111111111111",),
        )

    def test_panel_cutoff_rejects_future_panel(self) -> None:
        connection = self._PanelConnection(
            {
                "briefing": {
                    "kind": "close",
                    "observedAt": (
                        datetime.now(UTC) + timedelta(minutes=6)
                    ).isoformat(),
                }
            }
        )

        with self.assertRaisesRegex(ValueError, "future"):
            _panel_cutoff(
                connection, "11111111-1111-4111-8111-111111111111"
            )

    def test_symbol_trace_preserves_reason_transitions_and_error_detail(self) -> None:
        first = datetime(2026, 8, 25, 0, 1, tzinfo=UTC)
        second = datetime(2026, 8, 25, 0, 5, tzinfo=UTC)
        rows = (
            (
                "rule",
                first,
                json.dumps(
                    {
                        "symbols": [
                            {"symbol": "006340", "skipReason": "waiting:first-session-bar"}
                        ]
                    }
                ),
            ),
            (
                "rule",
                second,
                json.dumps(
                    {
                        "symbols": [
                            {
                                "symbol": "006340",
                                "skipReason": "invalid-stop",
                                "skipDetail": {"entry": "12610", "stop": "12820"},
                                "error": None,
                            }
                        ]
                    }
                ),
            ),
        )

        trace = _symbol_reason_traces(rows, ("006340",))["006340"]["rule"]

        self.assertEqual(
            trace["reasonPath"], ["waiting:first-session-bar", "invalid-stop"]
        )
        self.assertEqual(trace["transitionCount"], 1)
        self.assertEqual(trace["transitions"][-1]["detail"]["stop"], "12820")

    def test_minute_evidence_returns_only_compact_key_bars(self) -> None:
        rows = (
            (
                "006340",
                datetime(2026, 8, 25, 0, 1, tzinfo=UTC),
                Decimal(12610),
                Decimal(12620),
                Decimal(12590),
                Decimal(12600),
            ),
            (
                "006340",
                datetime(2026, 8, 25, 0, 5, tzinfo=UTC),
                Decimal(12650),
                Decimal(12680),
                Decimal(12640),
                Decimal(12670),
            ),
            (
                "006340",
                datetime(2026, 8, 25, 0, 6, tzinfo=UTC),
                Decimal(12670),
                Decimal(12690),
                Decimal(12660),
                Decimal(12680),
            ),
        )

        evidence = _minute_evidence(rows, ("006340", "064760"))

        self.assertEqual(evidence["006340"]["barCount"], 3)
        self.assertEqual(len(evidence["006340"]["keyBars"]), 2)
        self.assertEqual(evidence["006340"]["lastBar"]["close"], "12680")
        self.assertEqual(evidence["064760"]["coverage"], "missing-1m")


class CycleStatusPayloadTest(unittest.TestCase):
    def test_cycle_status_exposes_idle_funnel_and_symbol_ma(self) -> None:
        insight = {
            "idleReason": "no-crossover",
            "newBuysAllowed": True,
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
        self.assertEqual(
            payload["cashWeight"], str(Decimal(623136) / Decimal(1000000))
        )


if __name__ == "__main__":
    unittest.main()
