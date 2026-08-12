# toss-trader

토스증권 Open API용 안전 우선 MVP입니다. 현재 제공 범위:

자동 실행·리스크·Hermes·Telegram 흐름은
[`docs/automatic-trading-scenario.md`](docs/automatic-trading-scenario.md)에
정리되어 있습니다.

- OAuth 2.0 Client Credentials 토큰 캐시
- 현재가, 1분/일 캔들, 계좌 목록, 보유 종목 조회
- `429 Retry-After` 기반 GET 재시도, 구조화된 API 오류
- MA 단기/장기 교차 신호
- 주문액·종목액·일일 매수·일일 손실·API 오류·장 마감·중복 신호 제한
- SQLite/PostgreSQL paper trading ledger
- 멱등 캔들 collector와 저장 데이터 기반 MA scanner
- watchlist 수집·MA 스캔·RiskManager·paper 체결 단일 사이클
- 장전 benchmark 시장 상태 분석과 설정 universe 종목 발굴
- 시장 데이터 SQLite 저장 및 공유 PostgreSQL 선택 지원
- 실주문 API와 실주문 CLI는 **없음**

구현은 2026-08-12에 공식 canonical OpenAPI
`https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`의 `v1.2.13`을
확인해 작성했습니다. `/api/v1/accounts`는 계좌 헤더가 필요 없고,
`/api/v1/holdings` 같은 계좌 범위 API는 `X-Tossinvest-Account`가 필요합니다.

## 안전 기본값

`TRADING_ENABLED=false`가 기본값입니다. 현재 버전에는 mutation 경로 자체가
없어서 값을 `true`로 바꿔도 실주문은 발생하지 않습니다. 다음 단계에서 live
executor를 추가하더라도 이 kill switch, 명시적 실행 플래그, 멱등성
`clientOrderId`를 모두 통과하도록 구현해야 합니다.

실제 자격증명은 `.env`에 저장하지 말고 Infisical에서 프로세스 환경으로
주입하세요. 변수명은 [.env.example](.env.example)에 정리되어 있습니다.
이 저장소는 Infisical `toss` 프로젝트에 연결되어 있으며 기본 환경은 `prod`,
secret path는 루트 `/`입니다. `TOSS_ACCOUNT_SEQ` 값은 현재 계좌의 `1`입니다.

Toss WTS의 `설정 → Open API → 허용 IP 관리`에는 실행 서버의 외부 IPv4도
등록해야 합니다. 미등록 IP는 OAuth token 발급 후에도 데이터 API가 `403`으로
차단됩니다. 현재 N100의 확인된 외부 IPv4는 `122.202.132.246`입니다.

## 실행

로컬 설치 없이:

```bash
PYTHONPATH=src python3 -m toss_trader config
PYTHONPATH=src python3 -m toss_trader prices 005930 AAPL
PYTHONPATH=src python3 -m toss_trader candles 005930 --interval 1m --count 60
PYTHONPATH=src python3 -m toss_trader accounts
PYTHONPATH=src python3 -m toss_trader holdings --symbol 005930
```

캔들 수집과 저장 데이터 스캔:

```bash
# 최초 MA20/MA60 계산용 일봉 61개 확보
infisical run --env=prod --path=/ -- \
  env PYTHONPATH=src python3 -m toss_trader collect-candles \
  005930 --interval 1d --count 61

# 같은 캔들을 다시 받아도 timestamp 기준 upsert되어 중복되지 않음
PYTHONPATH=src python3 -m toss_trader scan-ma \
  005930 --interval 1d --short-window 20 --long-window 60
```

PostgreSQL 키가 없으면 `MARKET_DB_PATH=data/market.db`에 저장합니다.
공유 PostgreSQL을 사용하려면 Infisical에 아래 키를 각각 추가합니다.

```dotenv
POSTGRES_HOST=postgres.example.internal
POSTGRES_PORT=5432
POSTGRES_USER=toss_trader
POSTGRES_PASSWORD=...
POSTGRES_DB=toss_trader
```

모든 키가 준비되면 `market_candles` 테이블과 최신 조회 인덱스를 자동
생성하고, paper trading은 같은 DB의 `paper_fills` 테이블을 사용합니다.
비밀번호를 Compose 파일이나 저장소에 직접 기록하지 마세요.

MA 교차 확인:

```bash
PYTHONPATH=src python3 -m toss_trader ma-signal 005930 10 10 10 12 \
  --short-window 2 --long-window 3
```

paper 주문:

```bash
PYTHONPATH=src python3 -m toss_trader paper-order \
  --signal-id ma-005930-20260812-001 \
  --symbol 005930 \
  --side BUY \
  --price 71000 \
  --quantity 3 \
  --reason "MA20 crossed above MA60"
```

watchlist paper 자동 실행 사이클:

```bash
infisical run --env=prod --path=/ -- \
  uv run toss-trader run-paper-cycle
```

기본 설정:

```dotenv
WATCHLIST_SYMBOLS=005930
STRATEGY_INTERVAL=1d
STRATEGY_SHORT_WINDOW=20
STRATEGY_LONG_WINDOW=60
PAPER_ORDER_QUANTITY=1
```

장전 시장분석·종목발굴:

```bash
infisical run --env=prod --path=/ -- \
  uv run toss-trader run-market-scan
```

```dotenv
MARKET_BENCHMARK_SYMBOLS=069500,229200
DISCOVERY_SYMBOLS=005930,000660,373220,207940,005380,000270,068270,105560,055550,035420,035720,006400,051910,028260,012330
DISCOVERY_TOP_N=10
```

평일 `08:30 KST` n8n workflow가 시장 상태와 상위 후보를 Telegram topic에
보낸다. 이 단계는 주문이나 paper 체결을 하지 않는다.

사이클은 각 종목을 독립 처리합니다. 한 종목 수집이 실패해도 다음 종목을
계속 처리하고 `summary.failed`를 증가시키며 exit code `3`을 반환합니다.
신호가 없으면 체결하지 않습니다. 동일 캔들의 동일 신호는 signal ID가 같아
중복 체결되지 않습니다.

Docker + Infisical:

```bash
infisical run --env=prod --path=/ -- docker compose run --rm trader prices 005930 AAPL
```

모니터링과 Telegram alert 배포에는 Infisical `prod`, `/`에 아래 값이
필요합니다. `TELEGRAM_TOPIC`은 forum topic 숫자 ID입니다.

```dotenv
GRAFANA_ADMIN_PASSWORD=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-100...
TELEGRAM_TOPIC=...
```

```bash
infisical run --env=prod --path=/ -- \
  env DOCKER_HOST=ssh://choi@dev.dgst.me:50022 \
  docker compose -p toss-trader up -d --build \
  metrics alertmanager prometheus grafana
```

Tailscale 접속 포트는 metrics `9108`, Prometheus `19090`, Alertmanager
`19093`, Grafana `13000`입니다. Telegram 비밀값으로 생성한 Alertmanager
설정은 container tmpfs에만 저장됩니다.

n8n은 평일 `08:30 KST`에 장전 시장분석·종목발굴 리포트, 국내 장 마감 뒤
`15:40 KST`에 paper cycle·마감 리포트를 실행한다.

```bash
infisical run --env=prod --path=/ -- docker compose run --rm trader \
  run-paper-cycle
```

`STRATEGY_INTERVAL=1m` 전략만 1분 주기를 사용하세요. 반복 수집해도 candle
primary key와 signal ID 때문에 중복 저장·중복 paper 체결되지 않습니다.

## 검증

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
docker compose build
docker compose run --rm trader config
```

## 다음 단계

1. Infisical `WATCHLIST_SYMBOLS`에 paper 대상 종목 추가
2. n8n scheduler로 2~4주 paper 성과·장애 데이터 축적
3. Prometheus metrics와 Grafana dashboard 추가
4. 별도 승인 후 micro-live executor 구현
