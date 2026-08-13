import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PaperMcpAssetsTest(unittest.TestCase):
    def test_compose_exposes_internal_paper_mcp_without_toss_credentials(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        block = compose.split("  paper-mcp:\n", 1)[1].split(
            "\n  hermes-analysis:\n", 1
        )[0]

        self.assertIn('command: ["serve-paper-mcp"]', block)
        self.assertIn('POSTGRES_PORT: ${POSTGRES_PORT:-5431}', block)
        self.assertIn('PAPER_INITIAL_CASH: ${PAPER_INITIAL_CASH:-1000000}', block)
        self.assertIn('aliases:\n          - toss-trader-paper-mcp', block)
        self.assertIn('expose:\n      - "8090"', block)
        self.assertIn("read_only: true", block)
        self.assertNotIn("ports:", block)
        self.assertNotIn("TOSS_CLIENT", block)
        self.assertNotIn("TOSS_ACCOUNT", block)
        self.assertNotIn("TOSS_API", block)

    def test_analysis_sidecar_remains_zero_tool(self) -> None:
        config = (ROOT / "automation" / "hermes-analysis" / "config.yaml").read_text()

        self.assertIn("mcp_servers: {}", config)

    def test_docs_keep_paper_mcp_on_public_hermes_only(self) -> None:
        paper_mcp = (ROOT / "docs" / "paper-mcp.md").read_text()
        workflow = (ROOT / "docs" / "system-workflow.md").read_text()
        runbook = (ROOT / "docs" / "operations-runbook.md").read_text()
        scenario = (ROOT / "docs" / "automatic-trading-scenario.md").read_text()

        self.assertIn("toss_paper_status", paper_mcp)
        self.assertIn("toss_paper_holdings", paper_mcp)
        self.assertIn("toss_paper_pnl", paper_mcp)
        self.assertIn("mcp_servers: {}", paper_mcp)
        self.assertIn("hermes-analysis", paper_mcp)
        self.assertNotIn("TOSS_CLIENT", paper_mcp)
        self.assertIn("paper-mcp", workflow)
        self.assertIn("toss-trader-paper-mcp", runbook)
        self.assertIn("### 8. Telegram 질의", scenario)


if __name__ == "__main__":
    unittest.main()
