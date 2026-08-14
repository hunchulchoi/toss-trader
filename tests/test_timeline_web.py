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
            ("rule", "succeeded", "1d", datetime(2026, 8, 13, 0, tzinfo=UTC)),
            ("hermes", "failed", "1d", datetime(2026, 8, 13, 0, tzinfo=UTC)),
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

    def test_serves_read_only_page_assets_api_and_health(self) -> None:
        payload = _payload()

        root = timeline_response("GET", "/", payload)
        css = timeline_response("GET", "/assets/timeline.css", payload)
        script = timeline_response("GET", "/assets/timeline.js", payload)
        api = timeline_response("GET", "/api/timeline?ignored=1", payload)

        self.assertEqual(root[0], 200)
        self.assertIn(b'data-portfolio="rule"', root[2])
        self.assertIn(b".sparkline", css[2])
        self.assertIn(b"state.data.portfolios", script[2])
        self.assertEqual(json.loads(api[2])["portfolios"]["hermes"]["label"], "Hermes")
        self.assertEqual(timeline_response("GET", "/healthz", payload)[0], 200)
        self.assertEqual(timeline_response("GET", "/missing", payload)[0], 404)
        self.assertEqual(timeline_response("POST", "/api/timeline", payload)[0], 405)

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

        with self.assertRaises(ValueError):
            store.payload()
        self.assertIn("default_transaction_read_only=on", connection.connect_options)
        self.assertTrue(
            all(query.lstrip().upper().startswith("SELECT") for query in cursor.queries)
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
        self.assertEqual(serve.call_args.kwargs["payload"], payload)
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
        rows = [(), (), ()]
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
