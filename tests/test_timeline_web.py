import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from toss_trader.cli import main
from toss_trader.paper_timeline import PostgresPaperTimelineStore, build_paper_timeline
from toss_trader.timeline_web import timeline_response


def _payload():
    return build_paper_timeline(
        initial_rows=(("rule", "1000000"), ("hermes", "1000000")),
        fill_rows=(
            (
                "rule",
                "rule:samsung-buy",
                "005930",
                "BUY",
                "2",
                "70000",
                "140000",
                "21",
                "0",
                "rule entry",
                datetime(2026, 8, 13, 1, tzinfo=UTC),
            ),
            (
                "hermes",
                "hermes:hynix-buy",
                "000660",
                "BUY",
                "1",
                "250000",
                "250000",
                "37.5",
                "0",
                "hermes entry",
                datetime(2026, 8, 13, 2, tzinfo=UTC),
            ),
        ),
        mark_rows=(
            (
                "005930",
                "삼성전자",
                "68000",
                "KRW",
                datetime(2026, 8, 12, 6, tzinfo=UTC),
            ),
            (
                "000660",
                "SK하이닉스",
                "245000",
                "KRW",
                datetime(2026, 8, 12, 6, tzinfo=UTC),
            ),
            (
                "005930",
                "삼성전자",
                "71000",
                "KRW",
                datetime(2026, 8, 13, 6, tzinfo=UTC),
            ),
            (
                "000660",
                "SK하이닉스",
                "255000",
                "KRW",
                datetime(2026, 8, 13, 6, tzinfo=UTC),
            ),
            (
                "005930",
                "삼성전자",
                "72000",
                "KRW",
                datetime(2026, 8, 14, 6, tzinfo=UTC),
            ),
            (
                "000660",
                "SK하이닉스",
                "260000",
                "KRW",
                datetime(2026, 8, 14, 6, tzinfo=UTC),
            ),
        ),
        cycle_rows=(
            (
                "rule",
                "succeeded",
                "1m",
                datetime(2026, 8, 13, 0, tzinfo=UTC),
                None,
                "rule-run",
                datetime(2026, 8, 13, 0, 0, 12, tzinfo=UTC),
                15,
                0,
                0,
                0,
                0,
                "0",
                {
                    "idleReason": "setup-v2-block",
                    "newBuysAllowed": True,
                    "funnel": {"scanned": 15, "setupV2Blocked": 15},
                    "correction": {
                        "kind": "legacy-daily-snapshot-drift",
                        "universeApproved": 6,
                        "runtimeDailyCandidates": 5,
                        "symbols": ["090360"],
                    },
                    "reasons": {"setup-v2-block": 15},
                    "symbols": [
                        {
                            "symbol": "005930",
                            "reason": "setup-v2-block",
                            "skipReason": "setup-v2:violation:flow-not-confirmed",
                            "error": None,
                            "fillSide": None,
                        }
                    ],
                },
            ),
            (
                "hermes",
                "failed",
                "1m",
                datetime(2026, 8, 13, 0, tzinfo=UTC),
                "Hermes API timeout",
                "hermes-run",
                datetime(2026, 8, 13, 0, 0, 15, tzinfo=UTC),
                15,
                0,
                0,
                1,
                1,
                "0",
                None,
            ),
        ),
        risk_rows=(
            (
                "rule",
                "rule:samsung-buy",
                "005930",
                "BUY",
                "MA20/MA60 trend entry",
                True,
                [],
                datetime(2026, 8, 13, 1, tzinfo=UTC),
            ),
            (
                "hermes",
                "hermes:hynix-buy",
                "000660",
                "BUY",
                "MA20/MA60 trend entry",
                True,
                [],
                datetime(2026, 8, 13, 2, tzinfo=UTC),
            ),
            (
                "hermes",
                "hermes:hanmi-reject",
                "042700",
                "BUY",
                "MA20/MA60 trend entry",
                False,
                ["Hermes 거부"],
                datetime(2026, 8, 13, 3, tzinfo=UTC),
            ),
        ),
        advice_rows=(
            (
                "succeeded",
                None,
                {
                    "signalId": "hermes:hynix-buy",
                    "approved": True,
                    "rationale": "위험 한도 안입니다.",
                },
                datetime(2026, 8, 13, 2, 0, 1, tzinfo=UTC),
            ),
            (
                "succeeded",
                None,
                {
                    "signalId": "hermes:hanmi-reject",
                    "approved": False,
                    "rationale": "변동성 정보가 부족합니다.",
                },
                datetime(2026, 8, 13, 3, 0, 1, tzinfo=UTC),
            ),
        ),
        name_rows=(("042700", "한미반도체"),),
        minute_rows=(
            (
                "005930",
                "70000",
                "71200",
                "69800",
                "71000",
                "1000",
                "KRW",
                datetime(2026, 8, 13, 1, tzinfo=UTC),
            ),
            (
                "000660",
                "250000",
                "256000",
                "249000",
                "255000",
                "2000",
                "KRW",
                datetime(2026, 8, 13, 2, tzinfo=UTC),
            ),
        ),
        trend_rows=(
            ("005930", datetime(2026, 8, 12, 6, tzinfo=UTC), "68000"),
            ("005930", datetime(2026, 8, 13, 6, tzinfo=UTC), "71000"),
            ("005930", datetime(2026, 8, 14, 6, tzinfo=UTC), "72000"),
        ),
        hermes_log_rows=(
            (
                "trade-1",
                "hermes_trade",
                "succeeded",
                "decision",
                datetime(2026, 8, 13, 2, tzinfo=UTC),
                datetime(2026, 8, 13, 2, 0, 1, tzinfo=UTC),
                30,
                10,
                40,
                None,
                {
                    "symbol": "000660",
                    "side": "BUY",
                    "approved": True,
                    "rationale": "위험 한도 안입니다.",
                },
            ),
            (
                "daily-1",
                "daily",
                "succeeded",
                "completed",
                datetime(2026, 8, 13, 6, 40, tzinfo=UTC),
                datetime(2026, 8, 13, 6, 41, tzinfo=UTC),
                8000,
                200,
                8200,
                None,
                {"ok": True, "orchestrator": "n8n"},
            ),
        ),
        panel_rows=(
            (
                "panel-midday-1",
                "succeeded",
                datetime(2026, 8, 13, 2, 50, tzinfo=UTC),
                datetime(2026, 8, 13, 2, 50, 5, tzinfo=UTC),
                {"briefing": {"kind": "midday"}},
                None,
                "judge:hermes",
                "final judge",
                "openai",
                "gpt-5.6-terra",
                "점심 브리핑 판정입니다.",
                datetime(2026, 8, 13, 2, 50, 1, tzinfo=UTC),
                datetime(2026, 8, 13, 2, 50, 5, tzinfo=UTC),
                1000,
                200,
                1200,
            ),
        ),
        default_initial_cash=Decimal(1000000),
    )


class TimelineWebTest(unittest.TestCase):
    def test_builds_separate_rule_and_hermes_ledgers_with_names_and_trends(
        self,
    ) -> None:
        payload = _payload()

        self.assertEqual(payload["meta"]["scope"], "paper-only")
        self.assertEqual(set(payload["portfolios"]), {"rule", "hermes"})
        rule = payload["portfolios"]["rule"]["days"][-1]
        hermes = payload["portfolios"]["hermes"]["days"][-1]
        self.assertEqual(rule["positions"][0]["name"], "삼성전자")
        self.assertEqual(hermes["positions"][0]["name"], "SK하이닉스")
        self.assertEqual(len(rule["positions"][0]["priceTrend"]), 3)
        self.assertNotEqual(rule["equity"], hermes["equity"])
        self.assertEqual(payload["comparison"][-1]["date"], "2026-08-14")
        self.assertEqual(payload["decisions"][0]["outcome"], "bought")
        rejected = next(
            event
            for event in payload["decisions"]
            if event["signalId"] == "hermes:hanmi-reject"
        )
        self.assertEqual(rejected["outcome"], "rejected")
        self.assertEqual(rejected["hermes"]["rationale"], "변동성 정보가 부족합니다.")
        self.assertEqual(
            payload["intraday"]["series"]["2026-08-13"]["005930"][0]["close"],
            "71000",
        )
        self.assertEqual(payload["errors"][0]["message"], "Hermes API timeout")
        runs = {run["runId"]: run for run in payload["cycleTimeline"]["runs"]}
        self.assertEqual(runs["rule-run"]["durationMs"], 12000)
        self.assertEqual(
            runs["rule-run"]["correction"]["universeApproved"],
            6,
        )
        self.assertEqual(
            runs["rule-run"]["symbolStates"][0]["name"],
            "삼성전자",
        )
        self.assertEqual(
            payload["cycleTimeline"]["trends"]["005930"][-1]["close"],
            "72000",
        )
        conversations = {item["runId"]: item for item in payload["hermesConversations"]}
        self.assertEqual(conversations["trade-1"]["assistant"], "위험 한도 안입니다.")
        self.assertFalse(conversations["trade-1"]["bodyMissing"])
        self.assertEqual(conversations["trade-1"]["kind"], "종목 판단")
        self.assertTrue(conversations["daily-1"]["bodyMissing"])
        self.assertIsNone(conversations["daily-1"]["assistant"])

    def test_empty_active_ledgers_keep_initial_cash_days(self) -> None:
        payload = build_paper_timeline(
            initial_rows=(("rule", "1000000"), ("hermes", "1000000")),
            fill_rows=(),
            mark_rows=(),
            cycle_rows=(),
            default_initial_cash=Decimal(1000000),
        )

        rule = payload["portfolios"]["rule"]["days"][-1]
        hermes = payload["portfolios"]["hermes"]["days"][-1]
        self.assertEqual(rule["equity"], Decimal(1000000))
        self.assertEqual(hermes["cash"], Decimal(1000000))
        self.assertEqual(rule["positions"], [])
        self.assertEqual(hermes["trades"], [])
        self.assertEqual(payload["comparison"][-1]["equityDelta"], "0")
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["hermesConversations"], [])

    def test_hourly_panel_is_exposed_as_hourly_hermes_conversation(self) -> None:
        observed = datetime(2026, 8, 26, 2, 3, tzinfo=UTC)
        payload = build_paper_timeline(
            initial_rows=(("rule", "1000000"), ("hermes", "1000000")),
            fill_rows=(),
            mark_rows=(),
            cycle_rows=(),
            hermes_log_rows=(
                (
                    "watch-1",
                    "hourly_market_watch",
                    "succeeded",
                    "no-anomaly",
                    observed,
                    observed,
                    0,
                    0,
                    0,
                    None,
                    {"assistant": "시간별 자동 점검: 새 특이사항 없음"},
                ),
            ),
            panel_rows=(
                (
                    "panel-hourly-1",
                    "succeeded",
                    observed,
                    observed,
                    {"briefing": {"kind": "hourly"}},
                    None,
                    "judge:hermes",
                    "hourly anomaly judge",
                    "openai",
                    "gpt-5.6-terra",
                    "시간별 특이사항 검토",
                    observed,
                    observed,
                    100,
                    20,
                    120,
                ),
            ),
            default_initial_cash=Decimal(1000000),
        )

        items = {item["runId"]: item for item in payload["hermesConversations"]}
        self.assertEqual(items["panel-hourly-1"]["runType"], "hourly")
        self.assertEqual(items["panel-hourly-1"]["kind"], "시간별 감시")
        self.assertEqual(
            items["panel-hourly-1"]["assistant"], "시간별 특이사항 검토"
        )
        self.assertEqual(items["watch-1"]["runType"], "hourly")
        self.assertIn("새 특이사항 없음", items["watch-1"]["assistant"])

    def test_hunter_timeline_calculates_outcome_and_hermes_subset(self) -> None:
        evaluated_at = datetime(2026, 8, 25, 1, 1, tzinfo=UTC)
        plan = {
            "sessionDate": "2026-08-25",
            "ruleVersion": "momentum-shadow-v2",
            "selected": [
                {
                    "symbol": "005930",
                    "entryAt": datetime(2026, 8, 25, 1, tzinfo=UTC).isoformat(),
                    "entryPrice": "100",
                    "stopPrice": "98",
                    "targetPrice": "103",
                }
            ],
        }
        logs = (
            (
                "eval-1", "momentum-shadow", "succeeded", "evaluated",
                evaluated_at, evaluated_at, 0, 0, 0, None, plan,
            ),
            (
                "advice-1", "momentum-shadow-advice", "succeeded", "decision",
                evaluated_at, evaluated_at, 40, 10, 50, None,
                {
                    "sessionDate": "2026-08-25",
                    "ruleVersion": "momentum-shadow-v2",
                    "decisions": [
                        {
                            "symbol": "005930",
                            "verdict": "approve",
                            "rationale": "재돌파 유지",
                        }
                    ],
                },
            ),
        )
        minutes = (
            (
                "005930", "100", "103", "99", "102", "1000", "KRW",
                datetime(2026, 8, 25, 1, tzinfo=UTC),
            ),
        )

        payload = build_paper_timeline(
            initial_rows=(("rule", "1000000"), ("hermes", "1000000")),
            fill_rows=(),
            mark_rows=(),
            cycle_rows=(),
            name_rows=(("005930", "삼성전자"),),
            momentum_log_rows=logs,
            momentum_minute_rows=minutes,
            default_initial_cash=Decimal(1000000),
        )

        session = payload["momentumShadow"]["sessions"][0]
        self.assertEqual(session["meanReturnRate"], "0.03")
        self.assertEqual(session["hermesApprovedMeanReturnRate"], "0.03")
        self.assertEqual(session["candidates"][0]["outcome"]["status"], "target")
        self.assertEqual(session["candidates"][0]["hermes"]["totalTokens"], 50)

    def test_serves_read_only_page_assets_api_and_health(self) -> None:
        payload = _payload()

        root = timeline_response("GET", "/", payload)
        cycles = timeline_response("GET", "/cycles", payload)
        hermes = timeline_response("GET", "/hermes", payload)
        css = timeline_response("GET", "/assets/timeline.css", payload)
        cycle_css = timeline_response("GET", "/assets/cycles.css", payload)
        hermes_css = timeline_response("GET", "/assets/hermes.css", payload)
        script = timeline_response("GET", "/assets/timeline.js", payload)
        cycle_script = timeline_response("GET", "/assets/cycles.js", payload)
        hermes_script = timeline_response("GET", "/assets/hermes.js", payload)
        api = timeline_response("GET", "/api/timeline?ignored=1", payload)

        self.assertEqual(root[0], 200)
        self.assertIn(b'data-portfolio="rule"', root[2])
        self.assertIn(b'data-testid="hunter-shadow"', root[2])
        self.assertIn(b'data-testid="cycle-timeline"', cycles[2])
        self.assertIn(b'data-testid="hermes-log"', hermes[2])
        self.assertIn("시간별 감시".encode(), hermes[2])
        self.assertIn(b".sparkline", css[2])
        self.assertIn(b".cycle-row", cycle_css[2])
        self.assertIn(b".hermes-body", hermes_css[2])
        self.assertIn(b"state.data.portfolios", script[2])
        self.assertIn(b"momentumShadow", script[2])
        self.assertIn(b"cycleTimeline", cycle_script[2])
        self.assertIn("일봉 후보".encode(), cycle_script[2])
        self.assertIn("첫 분봉 대기".encode(), cycle_script[2])
        self.assertIn("선정 후 일봉변경".encode(), cycle_script[2])
        self.assertIn(b"hour12: false", cycle_script[2])
        self.assertIn(b"seoulToday", cycle_script[2])
        self.assertNotIn(b"key.slice(-5)", cycle_script[2])
        self.assertIn(b"hermesConversations", hermes_script[2])
        self.assertIn(b"date: today", hermes_script[2])
        self.assertIn(b"[today, ...conversations()", hermes_script[2])
        self.assertIn(b"https://www.tossinvest.com/stocks/A", script[2])
        self.assertIn(b"stock-order-link", css[2])
        self.assertEqual(json.loads(api[2])["portfolios"]["hermes"]["label"], "Hermes")
        self.assertEqual(timeline_response("GET", "/healthz", payload)[0], 200)
        self.assertEqual(timeline_response("GET", "/missing", payload)[0], 404)
        self.assertEqual(timeline_response("POST", "/api/timeline", payload)[0], 405)

    def test_api_resolves_fresh_payload_provider_per_request(self) -> None:
        calls = []

        def provider():
            calls.append(len(calls) + 1)
            return {"sequence": calls[-1]}

        first = timeline_response("GET", "/api/timeline", provider)
        second = timeline_response("GET", "/api/timeline", provider)

        self.assertEqual(json.loads(first[2]), {"sequence": 1})
        self.assertEqual(json.loads(second[2]), {"sequence": 2})

    def test_store_forces_postgres_read_only(self) -> None:
        cursor = _Cursor()
        connection = _Connection(cursor)
        store = PostgresPaperTimelineStore(
            {
                "host": "db",
                "port": 5431,
                "user": "reader",
                "password": "secret",
                "dbname": "toss_trader",
            },
            initial_cash=Decimal(1000000),
            connect=lambda **kwargs: connection,
        )

        payload = store.payload()
        self.assertEqual(set(payload["portfolios"]), {"rule", "hermes"})
        self.assertEqual(payload["portfolios"]["rule"]["days"][-1]["positions"], [])
        self.assertIn("default_transaction_read_only=on", connection.connect_options)
        self.assertTrue(
            all(query.lstrip().upper().startswith("SELECT") for query in cursor.queries)
        )
        self.assertTrue(
            any("market_scan" in query and "daily" in query for query in cursor.queries)
        )

    def test_cli_starts_rule_and_hermes_server(self) -> None:
        payload = _payload()
        output = StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "POSTGRES_HOST": "db",
                    "POSTGRES_PORT": "5431",
                    "POSTGRES_USER": "reader",
                    "POSTGRES_PASSWORD": "secret",
                    "POSTGRES_DB": "toss_trader",
                },
                clear=True,
            ),
            patch("toss_trader.cli.PostgresPaperTimelineStore") as store,
            patch("toss_trader.cli.serve_timeline") as serve,
            redirect_stdout(output),
        ):
            store.return_value.payload.return_value = payload
            exit_code = main(["serve-paper-timeline", "--port", "8099"])

        status = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(status["portfolios"], ["rule", "hermes"])
        self.assertIs(serve.call_args.kwargs["payload"], store.return_value.payload)
        self.assertEqual(serve.call_args.kwargs["port"], 8099)


class _Cursor:
    def __init__(self) -> None:
        self.queries = []
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, _parameters=None):
        self.queries.append(query)

    def fetchall(self):
        rows = [(), (), (), (), (), (), (), (), ()]
        value = rows[self._index]
        self._index += 1
        return value


class _Connection:
    connect_options = "-c default_transaction_read_only=on"

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
