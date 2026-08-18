import unittest
from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.advisor import HermesTradeAdvisor
from toss_trader.automation import HermesAnalysis
from toss_trader.models import Side, TradeSignal
from toss_trader.paper import PaperLedger
from toss_trader.risk import RiskContext


class StubAnalyzer:
    def __init__(self, result: HermesAnalysis | Exception) -> None:
        self.result = result
        self.payloads: list[dict[str, object]] = []

    def analyze(self, payload: dict[str, object]) -> HermesAnalysis:
        self.payloads.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class HermesTradeAdvisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = PaperLedger(":memory:", portfolio_id="hermes")
        self.signal = TradeSignal(
            signal_id="hermes:signal-1",
            symbol="005930",
            side=Side.BUY,
            reference_price=Decimal(70000),
            quantity=Decimal(1),
            reason="ma20 above ma60",
        )
        self.context = RiskContext(
            market_is_business_day=True,
            now=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
            position_notional=Decimal(0),
        )

    def tearDown(self) -> None:
        self.ledger.close()

    def test_records_json_decision_and_token_usage(self) -> None:
        analyzer = StubAnalyzer(
            HermesAnalysis(
                content='{"approved": false, "rationale": "거래량 확인 필요"}',
                prompt_tokens=30,
                completion_tokens=10,
                total_tokens=40,
            )
        )
        advisor = HermesTradeAdvisor(
            analyzer=analyzer,  # type: ignore[arg-type]
            audit=self.ledger,
            symbol_names={"005930": "삼성전자"},
        )

        advice = advisor.advise(self.signal, self.context)

        self.assertFalse(advice.approved)
        self.assertEqual(analyzer.payloads[0]["signal"]["name"], "삼성전자")  # type: ignore[index]
        self.assertNotIn("apiKey", str(analyzer.payloads[0]))
        run = self.ledger.recent_automation_runs(run_type="hermes_trade")[0]
        self.assertEqual(run["totalTokens"], 40)
        self.assertEqual(run["details"]["rationale"], "거래량 확인 필요")

    def test_records_failure_and_raises(self) -> None:
        advisor = HermesTradeAdvisor(
            analyzer=StubAnalyzer(RuntimeError("offline")),  # type: ignore[arg-type]
            audit=self.ledger,
            symbol_names={"005930": "삼성전자"},
        )

        with self.assertRaisesRegex(RuntimeError, "offline"):
            advisor.advise(self.signal, self.context)

        run = self.ledger.recent_automation_runs(run_type="hermes_trade")[0]
        self.assertEqual(run["status"], "failed")

    def test_rejects_ambiguous_symbol_without_company_name(self) -> None:
        advisor = HermesTradeAdvisor(
            analyzer=StubAnalyzer(HermesAnalysis(content="unused")),  # type: ignore[arg-type]
            audit=self.ledger,
            symbol_names={},
        )

        with self.assertRaisesRegex(RuntimeError, "company name missing"):
            advisor.advise(self.signal, self.context)


class PortfolioIsolationTest(unittest.TestCase):
    def test_rule_and_hermes_ledgers_keep_separate_cash_and_positions(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            rule = PaperLedger(path, portfolio_id="rule")
            hermes = PaperLedger(path, portfolio_id="hermes")
            rule.execute(self._signal("rule:one"))

            self.assertEqual(rule.cash_balance(Decimal(1000000)), Decimal(929990))
            self.assertEqual(hermes.cash_balance(Decimal(1000000)), Decimal(1000000))
            self.assertEqual(rule.position_quantity("005930"), Decimal(1))
            self.assertEqual(hermes.position_quantity("005930"), Decimal(0))
            rule.close()
            hermes.close()

    @staticmethod
    def _signal(signal_id: str) -> TradeSignal:
        return TradeSignal(
            signal_id=signal_id,
            symbol="005930",
            side=Side.BUY,
            reference_price=Decimal(70000),
            quantity=Decimal(1),
            reason="test",
        )


if __name__ == "__main__":
    unittest.main()
