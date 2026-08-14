from datetime import UTC, datetime
from decimal import Decimal

from toss_trader.paper_timeline import build_paper_timeline
from toss_trader.timeline_web import create_timeline_server

payload = build_paper_timeline(
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
        ("005930", "삼성전자", "68000", "KRW", datetime(2026, 8, 12, 6, tzinfo=UTC)),
        ("000660", "SK하이닉스", "245000", "KRW", datetime(2026, 8, 12, 6, tzinfo=UTC)),
        ("005930", "삼성전자", "71000", "KRW", datetime(2026, 8, 13, 6, tzinfo=UTC)),
        ("000660", "SK하이닉스", "255000", "KRW", datetime(2026, 8, 13, 6, tzinfo=UTC)),
        ("005930", "삼성전자", "72000", "KRW", datetime(2026, 8, 14, 6, tzinfo=UTC)),
        ("000660", "SK하이닉스", "260000", "KRW", datetime(2026, 8, 14, 6, tzinfo=UTC)),
    ),
    cycle_rows=(
        ("rule", "succeeded", "1d", datetime(2026, 8, 13, tzinfo=UTC)),
        ("hermes", "failed", "1d", datetime(2026, 8, 13, tzinfo=UTC)),
    ),
    default_initial_cash=Decimal(1000000),
)

with create_timeline_server(host="127.0.0.1", port=18091, payload=payload) as server:
    server.serve_forever()
