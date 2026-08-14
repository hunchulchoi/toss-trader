import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PaperMcpAssetsTest(unittest.TestCase):
    def test_compose_exposes_internal_paper_mcp_without_toss_credentials(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        block = compose.split("  paper-mcp:\n", 1)[1].split("\n  timeline:\n", 1)[0]

        self.assertIn('command: ["serve-paper-mcp"]', block)
        self.assertIn("POSTGRES_PORT: ${POSTGRES_PORT:-5431}", block)
        self.assertIn(
            "POSTGRES_USER: ${TOSS_MCP_POSTGRES_USER:-toss_mcp_reader}", block
        )
        self.assertIn("TOSS_MCP_POSTGRES_PASSWORD", block)
        self.assertIn("PAPER_INITIAL_CASH: ${PAPER_INITIAL_CASH:-1000000}", block)
        self.assertIn("aliases:\n          - toss-trader-paper-mcp", block)
        self.assertIn('expose:\n      - "8090"', block)
        self.assertIn("read_only: true", block)
        self.assertNotIn("ports:", block)
        self.assertNotIn("TOSS_CLIENT", block)
        self.assertNotIn("TOSS_ACCOUNT", block)
        self.assertNotIn("TOSS_API", block)

    def test_analysis_sidecar_remains_zero_tool(self) -> None:
        config = (ROOT / "automation" / "hermes-analysis" / "config.yaml").read_text()

        self.assertIn("mcp_servers: {}", config)

    def test_telegram_policy_blocks_real_account_lookup(self) -> None:
        soul = (ROOT / "automation" / "hermes-telegram" / "SOUL.md").read_text()

        self.assertIn("use only the read-only `toss-paper` MCP tools", soul)
        self.assertIn("Never use `terminal`", soul)
        self.assertIn("`toss-trader holdings`", soul)
        self.assertIn("refuse briefly", soul)
        self.assertIn("idleReason", soul)
        self.assertIn("no-crossover", soul)
        self.assertIn("already-held", soul)

    def test_reader_migration_enforces_select_only_role(self) -> None:
        migration = (ROOT / "db" / "paper_mcp_reader.sql").read_text()

        self.assertIn("CREATE ROLE toss_mcp_reader", migration)
        self.assertIn("NOINHERIT", migration)
        self.assertIn("default_transaction_read_only", migration)
        self.assertIn("GRANT SELECT ON ALL TABLES", migration)
        self.assertIn("ALTER DEFAULT PRIVILEGES FOR ROLE toss_trader", migration)
        self.assertNotIn("GRANT INSERT", migration)
        self.assertNotIn("GRANT UPDATE", migration)
        self.assertNotIn("GRANT DELETE", migration)

    def test_docs_keep_paper_mcp_on_public_hermes_only(self) -> None:
        paper_mcp = (ROOT / "docs" / "paper-mcp.md").read_text()
        workflow = (ROOT / "docs" / "system-workflow.md").read_text()
        runbook = (ROOT / "docs" / "operations-runbook.md").read_text()
        scenario = (ROOT / "docs" / "automatic-trading-scenario.md").read_text()

        self.assertIn("toss_paper_status", paper_mcp)
        self.assertIn("toss_paper_holdings", paper_mcp)
        self.assertIn("toss_paper_pnl", paper_mcp)
        self.assertIn("idleReason", paper_mcp)
        self.assertIn("symbolStates", paper_mcp)
        self.assertIn("changelog.md", workflow)
        changelog = (ROOT / "docs" / "changelog.md").read_text()
        self.assertIn("2026-08-14", changelog)
        self.assertIn("idleReason", changelog)
        self.assertIn("continuation", changelog)
        self.assertIn("mcp_servers: {}", paper_mcp)
        self.assertIn("hermes-analysis", paper_mcp)
        self.assertNotIn("TOSS_CLIENT", paper_mcp)
        self.assertIn("paper-mcp", workflow)
        self.assertIn("toss-trader-paper-mcp", runbook)
        self.assertIn("### 8. Telegram 질의", scenario)


if __name__ == "__main__":
    unittest.main()
