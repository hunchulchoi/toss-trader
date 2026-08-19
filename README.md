# toss-trader

토스증권 Open API용 안전 우선 MVP입니다. 현재 제공 범위:

자동 실행·리스크·Hermes·Telegram 흐름은
[`docs/automatic-trading-scenario.md`](docs/automatic-trading-scenario.md)에
정리되어 있습니다.
Hermes Telegram의 paper 장부 조회는
[`docs/paper-mcp.md`](docs/paper-mcp.md)를 참고합니다.
날짜별 기능 추가는 [`docs/changelog.md`](docs/changelog.md).

- OAuth 2.0 Client Credentials 토큰 캐시
- 현재가, 1분/일 캔들, 계좌 목록, 보유 종목 조회
- `429 Retry-After` 기반 GET 재시도, 구조화된 API 오류
- MA 단기/장기 교차 신호
- 주문액·종목액·일일 매수·일일 손실·API 오류·장 마감·중복 신호 제한
- SQLite/PostgreSQL paper trading ledger
- 멱등 캔들 collector와 저장 데이터 기반 MA scanner
- watchlist 수집·MA 스캔·RiskManager·paper 체결 단일 사이클
- 평일 장중 5분 간격 setup-v2.2 paper cycle
- 장전 benchmark 시장 상태 분석과 설정 universe 종목 발굴
- RiskManager 판단·자동화 실행·Hermes token 감사 장부
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
기본 운영 환경은 Infisical `prod`, secret path는 루트 `/`입니다.
`TOSS_ACCOUNT_SEQ`는 대상 계좌에 맞는 환경값으로 주입합니다.

Toss WTS의 `설정 → Open API → 허용 IP 관리`에는 실행 서버의 현재 외부 IPv4를
등록해야 합니다. 미등록 IP는 OAuth token 발급 후에도 데이터 API가 `403`으로
차단됩니다. IP는 배포·장애 시점에 서버와 WTS 설정에서 다시 확인합니다.

## 실행

로컬 설치 없이:

```bash
PYTHONPATH=src python3 -m toss_trader config
PYTHONPATH=src python3 -m toss_trader prices 005930 AAPL
PYTHONPATH=src python3 -m toss_trader candles 005930 --interval 1m --count 60
PYTHONPATH=src python3 -m toss_trader accounts
PYTHONPATH=src python3 -m toss_trader holdings --symbol 005930
```

CLI `holdings`는 Toss 실계좌다. paper Rule/Hermes 보유·손익은 Telegram 공용
Hermes MCP로 조회한다. [`docs/paper-mcp.md`](docs/paper-mcp.md).

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
POSTGRES_PORT=5431
POSTGRES_USER=toss_trader
POSTGRES_PASSWORD=...
POSTGRES_DB=toss_trader
```

모든 키가 준비되면 시세, paper trading, 공식 PIT 저장소를 같은 PostgreSQL에
생성합니다. `market_universe_raw_v2`와 `market_flow_pit_v2`는 `session_date`
기준 월별 파티션이며, 나머지 PIT 테이블은 일반 테이블입니다.
비밀번호를 Compose 파일이나 저장소에 직접 기록하지 마세요.

운영 Toss DB는 `common-postgres`의 호스트 포트 `5431`만 사용합니다.
`5432`의 `dgst_postgres`는 DGST 전용이므로 Toss Trader를 연결하지 않습니다.

MA 교차 확인:

```bash
PYTHONPATH=src python3 -m toss_trader ma-signal 005930 10 10 10 12 \
  --short-window 2 --long-window 3
```

저장 캔들 MA 백테스트:

```bash
PYTHONPATH=src python3 -m toss_trader backtest-ma 005930 \
  --interval 1d --count 1000 --short-window 20 --long-window 60 \
  --quantity 1 --slippage-bps 5 --initial-cash 1000000
```

운영 장부에는 쓰지 않으며 paper trading과 같은 Toss 수수료·세금을 반영한다.
신호 다음 캔들 시가에 체결하고 Buy & Hold 대비 초과수익률도 출력한다.
체결 가정과 결과 필드는 [MA 백테스트](docs/backtesting.md)를 참고한다.

여러 종목을 하나의 현금 잔액으로 재생:

```bash
PYTHONPATH=src python3 -m toss_trader backtest-portfolio-ma \
  005930 000660 069500 \
  --interval 1d --count 1000 \
  --short-window 20 --long-window 60 \
  --quantity 1 --slippage-bps 5 \
  --initial-cash 10000000 --format json
```

종목별 독립 신호, 공유 현금, 다음 시가 체결, Toss 수수료·세금, 동일가중
Buy & Hold 비교와 JSON/CSV 출력은 [MA 백테스트](docs/backtesting.md)에 정리한다.

Rule/Hermes paper 장부 타임라인 웹페이지:

```bash
PYTHONPATH=src python3 -m toss_trader serve-paper-timeline \
  --host 127.0.0.1 --port 8091
```

실계좌를 조회하지 않는다. PostgreSQL의 Rule/Hermes paper 체결과 저장 시세를
읽기 전용으로 재생하며, 회사명과 종목별 최근 63개 시세 추세선을 함께 표시한다.
`/cycles`는 cycle 실행 흐름, `/hermes`는 Hermes 판단·분석 응답이다.
활성 세대에 체결이 없어도 초기현금 날짜로 페이지를 연다.

날짜를 선택하면 해당일 총자산·현금·보유 평가액·손익·종목별 장부·체결을
확인할 수 있다. compose 서비스는 Tailscale `${TIMELINE_PORT:-19094}`에만
바인딩하며 SELECT-only `toss_mcp_reader`를 사용한다.

MA 조합 학습/검증:

```bash
PYTHONPATH=src python3 -m toss_trader walk-forward-ma 005930 \
  --count 1000 --short-windows 5 10 20 --long-windows 20 40 60 \
  --train-ratio 0.7 --slippage-bps 5 --format csv
```

KRX 정보데이터시스템에서 같은 날짜·전체 종목으로 각각 내려받은 외국인 및
기관합계 CSV는 첫 관측 시각을 보존해 공식 수급 원장으로 가져올 수 있다.

```bash
infisical run --env=prod --path=/ -- toss-trader import-krx-flow-csv \
  --session-date 2026-08-18 \
  --foreign-csv /path/to/foreign.csv \
  --institutional-csv /path/to/institutional.csv \
  --trading-csv /path/to/all-stocks.csv
```

두 파일에 모두 있는 전체 6자리 국내 종목 중 해당 세션의 공식 거래대금이 있는
행을 저장한다. 한쪽 파일에서 빠진 종목은 setup-v2에서 계속 fail-closed한다.
`available_at`은 실제 import 시각이며 과거로 소급하지 않는다. 동일 세션에
KIS와 KRX가 모두 있으면 setup-v2는 KRX를 우선한다.

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
  env PYTHONPATH=src python3 -m toss_trader run-paper-cycle

# 운영 장중 endpoint와 동일한 1분봉 실행
infisical run --env=prod --path=/ -- \
  env PYTHONPATH=src python3 -m toss_trader run-paper-cycle --interval 1m
```

기본 설정:

```dotenv
STRATEGY_INTERVAL=1d
STRATEGY_SHORT_WINDOW=20
STRATEGY_LONG_WINDOW=60
PAPER_ORDER_QUANTITY=1
PAPER_INITIAL_CASH=1000000
CANDLE_REQUEST_INTERVAL_SECONDS=0.25
DYNAMIC_UNIVERSE_CANDIDATE_COUNT=30
DYNAMIC_UNIVERSE_RANKING_FETCH_COUNT=100
DYNAMIC_UNIVERSE_SIZE=15
```

`STRATEGY_INTERVAL=1d`는 수동 실행과 15:40 마감 cycle의 기본값이다. 장중
`/run-paper-cycle` endpoint는 환경값과 관계없이 `--interval 1m`을 강제한다.
현재 신호 상태기계의 기준 문서는
[`docs/paper-cycle-flow.md`](docs/paper-cycle-flow.md)다. 1m v2 cycle은 분봉과
함께 완결 일봉이 200개가 되거나 provider cursor가 소진될 때까지 bounded
pagination한다. cursor 무진전·페이지 상한·부분 이력 뒤 빈 응답은 데이터 오류로
재시도하고, 정상 응답으로 확인된 완결 일봉이 200개보다 적을 때만 skip한다.
종목은 고정 watchlist가 아니다. 서울 거래일 첫 cycle에서 Toss 실시간 거래대금
랭킹을 최대 100개 조회한다. STOCK·보통주·ACTIVE·거래정상 종목만 다시 순위를
매겨 상위 30개의 직전 완결 일봉 200개를 검사하고, setup-v2.2 가격 조건 통과
종목을 최대 15개까지 고정한다. `TOP_GAINERS`는 선정에 쓰지 않는다. 부족분을
채우지 않으며 정상 평가 결과 0종도 성공이다. 랭킹·metadata·가격 데이터 오류는
성공 cache로 저장하지 않고 다음 cycle에서 재시도한다. 현금·주문 한도·일일 손실·
API 오류 같은 가변 Risk는 membership이 아니라 BUY 실행 단계에서 검사한다.
정적 membership은 로컬 순수 검증이라 후보 수만큼 n8n을 호출하지 않는다.
BUY 후보는 MA 교차가 아니라 직전 완결 일봉의 setup-v2.2 가격 조건과 PIT
수급·이벤트에서 직접 만든다. D+1 첫 완결 1분봉에서 3% 갭과 위험 수량을 다시
검사하며, 누락은 `setup-v2-block`으로 차단한다. 이벤트는 OpenDART, 수급은
같은 세션에서 KRX를 KIS first-observed보다 우선한다. 수급 6세션이 찰 때까지
신규 BUY 0건이 정상이다. 현재 paper 실험값은 하루 최대 매수 5회, 동시
보유 10종목이다. 이 값은 2026-08-14 장중 변경 뒤 아직 온전한 거래일 검증을
거치지 않은 미확정 값이다.
보유 종목은 순위에서 빠져도 계속 추적한다. 랭킹 장애 시 보유 종목만 추적하고
신규 BUY는 `universe-refresh-failed`로 거부한다. `/candles`는 기본 0.25초 간격과
Toss rate-limit 응답 헤더에 따라 더 느리게 호출한다.

장전 시장분석·종목발굴:

```bash
infisical run --env=prod --path=/ -- \
  env PYTHONPATH=src python3 -m toss_trader run-market-scan
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

paper 체결 비용은 2026-08 토스증권 Open API 일반 요율을 따른다. 6자리 숫자
심볼은 KRX 국내 보통주로 보고 매수·매도 수수료 0.015%, 매도 거래세 0.20%를
원 미만 절사한다. 영문 심볼은 미국주식으로 보고 수수료 0.1%를 센트 미만
절사하며, 주문 총 체결액이 $10 이하면 면제한다. 비용은 `paper_fills`의
`commission`, `tax`에 기록되고 현금잔고와 매수 가능 금액에 반영된다. 현재
체결 모델에 거래소·상품유형이 없으므로 NXT 0.014%, 국내 ETF 거래세 면제,
계정별 프로모션 요율은 지원하지 않는다.

포지션 원가는 체결 순서의 이동평균법으로 계산한다. 부분매도 시 매도 수량에
해당하는 평균원가를 제거하고 수수료·세금을 차감한 실현손익을 기록한다.
각 cycle은 `paper_portfolio_snapshots`에 총자산·실현손익·미실현손익·누적비용을
저장한다. `daily-loss-limit`은 UTC 일자별 `paper_portfolio_daily_baselines`의
시작 총자산 대비 현재 총자산 수익률을 사용하며 신규 매수만 차단한다.
손실 한도 이후에도 보유 포지션 매도는 허용한다.
계산식과 장부 흐름은 [`docs/pnl-engine.md`](docs/pnl-engine.md)에 정리되어 있다.

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
N8N_RISK_MANAGER_TOKEN=...
N8N_MANUAL_TRIGGER_TOKEN=...
```

```bash
infisical run --env=prod --path=/ -- \
  env DOCKER_HOST=ssh://choi@dev.dgst.me:50022 \
  docker compose -p toss-trader up -d --build \
  metrics alertmanager prometheus
```

Tailscale 접속 포트는 metrics `9108`, Prometheus `19090`, Alertmanager
`19093`입니다. dashboard는 공용 Grafana `3001`의 `Trading` 폴더를 사용한다.
Telegram 비밀값으로 생성한 Alertmanager 설정은 container tmpfs에만 저장됩니다.

운영 스케줄:

| 시각(KST) | 작업 | Hermes | Telegram |
|---|---|---|---|
| 평일 08:30 | 시장분석·종목발굴 | 시장 의견 생성 | 리포트 전송 |
| 평일 09:00~15:20, 5분 간격 | setup-v2.2 D+1 rule/Hermes 비교 | v2·한도 통과 신호만 advisor | 특이사항만 전송 |
| 평일 15:40 | 일봉 paper + 당일 1m v2 퍼널 마감 분석 | 규칙 준수 요약 | 마감 리포트 전송 |

장중 신호는 RiskManager 승인 후에만 100만원 가상 장부에 반영한다. 휴장일에는
매수·매도를 모두 거부하며, 실제 주문은 실행하지 않는다. 정상 무신호 cycle은
Telegram을 보내지 않는다.

paper cycle에서 체결, RiskManager 거부, 종목 처리 실패, Toss API 연속 오류,
일일 손실 한도가 감지되면 `TossTraderPaperCycleNotice`로 즉시 Telegram에
추가 보고한다. 정상 무신호 cycle, 정상적인 `duplicate-signal` 재실행,
슬롯 한도 `max-open-positions` 단독 거부는 추가 보고하지 않는다. 한도
판단 자체는 장부에 남는다.

```bash
infisical run --env=prod --path=/ -- docker compose run --rm trader \
  run-paper-cycle

# 판단·실행·Hermes token 조회
docker exec toss-trader-automation-1 toss-trader risk-decisions --limit 20
docker exec toss-trader-automation-1 toss-trader automation-runs --limit 20

# 운영 n8n Task Broker와 충돌 없는 인증 Webhook 마감 리뷰 실행
./automation/n8n/run-daily-webhook.sh
```

장중 endpoint는 항상 1분봉을 사용한다. 반복 수집해도 candle primary key와
signal ID 때문에 중복 저장·중복 paper 체결되지 않는다.
Toss candle 조회는 성공했지만 MA20/MA60 계산 이력이 부족한 신규 종목은
`skipReason`을 남기고 해당 cycle의 실패나 API 오류 streak로 계산하지 않는다.

## 검증

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check .
infisical run --env=prod --path=/ -- docker compose config -q
```

## 다음 단계

1. n8n scheduler로 2~4주 paper 성과·장애 데이터 축적
2. 공용 Grafana에서 universe/Risk 판단·가상 체결·token 추이 검토
3. 실주문은 별도 설계·검증·명시적 승인 전까지 구현하지 않음

전체 실행 흐름, ERD, 파일별 책임은
[`docs/system-workflow.md`](docs/system-workflow.md)에 정리되어 있다.
날짜별 기능 추가는 [`docs/changelog.md`](docs/changelog.md).
수동 마감 리뷰, 배포 검증, 장애 대응은
[`docs/operations-runbook.md`](docs/operations-runbook.md)를 따른다.
