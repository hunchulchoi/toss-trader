import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from toss_trader.advisor import (
    HermesTradeAdvisor,
    hermes_market_review,
    review_momentum_shadow_once,
)
from toss_trader.automation import HermesAnalysis
from toss_trader.models import Candle, Side, TradeSignal
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

    def test_includes_compact_market_review_in_payload(self) -> None:
        analyzer = StubAnalyzer(
            HermesAnalysis(
                content='{"approved": true, "rationale": "되돌림과 수급 확인"}',
            )
        )
        advisor = HermesTradeAdvisor(
            analyzer=analyzer,  # type: ignore[arg-type]
            audit=self.ledger,
            symbol_names={"005930": "삼성전자"},
        )
        bar = Candle(
            symbol="005930",
            interval="1d",
            timestamp=datetime(2026, 8, 17, 6, tzinfo=UTC),
            open_price=Decimal(69000),
            high_price=Decimal(71000),
            low_price=Decimal(68000),
            close_price=Decimal(70000),
            volume=Decimal(1000),
            currency="KRW",
        )

        advisor.advise(
            self.signal,
            self.context,
            review=hermes_market_review(daily=(bar,), minutes=()),
        )

        market = analyzer.payloads[0]["market"]  # type: ignore[index]
        self.assertEqual(market["daily"][0]["c"], "70000")
        self.assertEqual(market["minutes"], [])
        self.assertNotIn("apiKey", str(analyzer.payloads[0]))

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


class MomentumShadowAdvisorTest(unittest.TestCase):
    def test_records_one_batched_non_trading_review_with_tokens(self) -> None:
        ledger = PaperLedger(":memory:", portfolio_id="rule")
        analyzer = StubAnalyzer(
            HermesAnalysis(
                content=(
                    '{"decisions":['
                    '{"symbol":"AAA","verdict":"approve","rationale":"유지력 확인"},'
                    '{"symbol":"BBB","verdict":"watch","rationale":"가속 확인 필요"}'
                    "]}"
                ),
                prompt_tokens=50,
                completion_tokens=20,
                total_tokens=70,
            )
        )
        payload = {
            "sessionDate": "2026-08-25",
            "ruleVersion": "momentum-shadow-v2",
            "selected": [{"symbol": "AAA"}, {"symbol": "BBB"}],
        }
        with patch("toss_trader.advisor.HermesAnalyzer", return_value=analyzer):
            first = review_momentum_shadow_once(
                api_key="x" * 20,
                base_url="http://hermes",
                audit=ledger,
                payload=payload,
                symbol_names={"AAA": "에이", "BBB": "비"},
            )
            second = review_momentum_shadow_once(
                api_key="x" * 20,
                base_url="http://hermes",
                audit=ledger,
                payload=payload,
                symbol_names={"AAA": "에이", "BBB": "비"},
            )

        self.assertEqual(first["decisions"][0]["verdict"], "approve")
        self.assertTrue(second["cacheHit"])
        self.assertEqual(len(analyzer.payloads), 1)
        run = ledger.recent_automation_runs(
            run_type="momentum-shadow-advice"
        )[0]
        self.assertEqual(run["totalTokens"], 70)
        self.assertFalse(run["details"]["strategyInput"])
        ledger.close()


if __name__ == "__main__":
    unittest.main()
