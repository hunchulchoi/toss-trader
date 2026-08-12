import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from toss_trader.models import Candle
from toss_trader.screening import (
    MarketRegime,
    MarketScanResult,
    analyze_market,
    discover_candidate,
    format_market_scan_report,
    market_scan_to_dict,
)


def candles(
    symbol: str,
    closes: list[int],
    *,
    latest_volume: int = 200,
) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    for index, close in enumerate(closes):
        price = Decimal(close)
        result.append(
            Candle(
                symbol=symbol,
                interval="1d",
                timestamp=start + timedelta(days=index),
                open_price=price,
                high_price=price,
                low_price=price,
                close_price=price,
                volume=Decimal(latest_volume if index == len(closes) - 1 else 100),
                currency="KRW" if symbol.isdigit() else "USD",
            )
        )
    return result


class MarketAnalysisTest(unittest.TestCase):
    def test_classifies_risk_on_off_and_neutral(self) -> None:
        rising = candles("069500", list(range(100, 160)))
        falling = candles("SPY", list(range(160, 100, -1)))
        flat = candles("QQQ", [100] * 60)

        self.assertEqual(analyze_market(rising).regime, MarketRegime.RISK_ON)
        self.assertEqual(analyze_market(falling).regime, MarketRegime.RISK_OFF)
        self.assertEqual(analyze_market(flat).regime, MarketRegime.NEUTRAL)

    def test_requires_sixty_daily_candles(self) -> None:
        with self.assertRaisesRegex(ValueError, "60 candles"):
            analyze_market(candles("069500", [100] * 59))


class DiscoveryTest(unittest.TestCase):
    def test_discovers_uptrend_and_scores_volume(self) -> None:
        candidate = discover_candidate(
            candles("005930", list(range(100, 160)), latest_volume=250)
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.symbol, "005930")
        self.assertGreater(candidate.momentum_20d, Decimal(0))
        self.assertEqual(candidate.volume_ratio, Decimal("2.5"))
        self.assertGreater(candidate.score, Decimal(0))

    def test_rejects_non_uptrend(self) -> None:
        self.assertIsNone(
            discover_candidate(candles("005930", list(range(160, 100, -1))))
        )

    def test_market_json_contains_display_names(self) -> None:
        market = analyze_market(candles("069500", list(range(100, 160))))
        candidate = discover_candidate(candles("068270", list(range(100, 160))))
        assert candidate is not None

        payload = market_scan_to_dict(
            MarketScanResult(
                markets=(market,), candidates=(candidate,), errors={}
            )
        )

        self.assertEqual(payload["markets"][0]["name"], "KOSPI200")
        self.assertEqual(payload["candidates"][0]["name"], "셀트리온")

    def test_formats_telegram_report(self) -> None:
        report = format_market_scan_report(
            {
                "exitCode": 0,
                "scan": {
                    "markets": [
                        {
                            "symbol": "069500",
                            "regime": "RISK_ON",
                            "momentum20d": "0.12",
                        }
                    ],
                    "candidates": [
                        {
                            "symbol": "005930",
                            "score": "14.5",
                            "momentum20d": "0.10",
                            "volumeRatio": "1.3",
                        }
                    ],
                    "errors": {},
                },
            },
            opinion="시장 추세 양호. 거래량 확인이 필요합니다.",
        )

        self.assertIn("📊 시장 분석\n", report)
        self.assertIn("• KOSPI200: RISK_ON", report)
        self.assertIn("🔎 발굴 종목\n", report)
        self.assertIn("1. 삼성전자 (005930)", report)
        self.assertIn("모멘텀 +10.00%\n   거래량 1.30x", report)
        self.assertIn("💬 Hermes 의견\n", report)
        self.assertIn("시장 추세 양호", report)
        self.assertIn("오류 0건", report)

    def test_formats_neutral_opinion_without_candidates(self) -> None:
        report = format_market_scan_report(
            {
                "exitCode": 0,
                "scan": {
                    "markets": [
                        {
                            "symbol": "069500",
                            "regime": "NEUTRAL",
                            "momentum20d": "-0.02",
                        }
                    ],
                    "candidates": [],
                    "errors": {},
                },
            },
            opinion="시장 방향성 불명확. 관망이 적절해 보입니다.",
        )

        self.assertIn("조건 충족 종목 없음", report)
        self.assertIn("방향성 불명확", report)

    def test_requires_non_empty_llm_opinion(self) -> None:
        with self.assertRaisesRegex(ValueError, "LLM opinion"):
            format_market_scan_report(
                {"exitCode": 0, "scan": {"markets": [], "candidates": []}},
                opinion=" ",
            )


if __name__ == "__main__":
    unittest.main()
