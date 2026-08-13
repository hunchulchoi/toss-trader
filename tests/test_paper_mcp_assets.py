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


if __name__ == "__main__":
    unittest.main()
