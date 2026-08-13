import unittest
from typing import Any

from toss_trader.paper_mcp import (
    PaperMcpService,
    PostgresPaperReadStore,
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


if __name__ == "__main__":
    unittest.main()
