# Toss Trader 운영 runbook

## 안전 경계

- 운영 대상은 paper trading뿐이다.
- `TRADING_ENABLED=false`를 항상 유지한다.
- 실제 주문 생성·정정·취소 기능은 없다.
- PostgreSQL volume, n8n volume, 운영 장부를 초기화하지 않는다.
- secret 값은 shell argument, 로그, 문서, Git에 출력하거나 저장하지 않는다.

## 마감 리뷰 수동 실행

운영 n8n 컨테이너 안에서 `n8n execute`를 실행하지 않는다. 두 번째 n8n
프로세스가 Task Broker 기본 port `5679`를 열려고 해 운영 프로세스와 충돌한다.

저장소의 인증 Webhook runner를 사용한다.

```bash
./automation/n8n/run-daily-webhook.sh
```

정상 접수 응답:

```json
{"message":"Workflow was started"}
```

Webhook은 reverse proxy 장기 연결 timeout을 피하도록 즉시 응답한다. 위 응답은
완료가 아니라 접수 성공이다. 완료는 다음 항목을 모두 확인한다.

1. n8n `Toss Trader Daily n8n Orchestration` execution이 `success`
2. `automation_run_logs`의 같은 execution ID에 `rule-cycle`, `hermes-cycle`,
   `hermes-analysis`, `telegram-report`가 모두 `succeeded`
3. rule·Hermes cycle의 `failed=0`, `exitCode=0`
4. `hermes-analysis`의 `totalTokens > 0`
5. `telegram-report`의 `telegramAccepted=true`
6. 실행 구간에 Alertmanager
   `alertmanager_notifications_failed_total{integration="telegram"}` 증가 없음
7. PostgreSQL relation lock과 `idle in transaction` 누적 없음

최근 stage는 다음 명령으로 확인한다.

```bash
docker exec toss-trader-automation-1 \
  toss-trader automation-runs --limit 30
```

n8n PostgreSQL에서는 상태만 read-only 조회한다. execution 행을 직접 수정하거나
삭제하지 않는다.

```bash
docker exec common-postgres psql -U n8n -d n8n -P pager=off -c \
  "SELECT id,status,mode,\"workflowId\",\"startedAt\",\"stoppedAt\"
   FROM execution_entity
   WHERE \"workflowId\"='toss-trader-daily-paper-hermes'
   ORDER BY id DESC LIMIT 5;"
```

Telegram 누적 실패 counter:

```bash
docker exec toss-trader-alertmanager-1 wget -qO- \
  http://127.0.0.1:9093/metrics |
  rg '^alertmanager_notifications_failed_total.*telegram'
```

## 인증 Webhook 배포

필수 secret은 Infisical `prod`, path `/`에 저장한다.

- `N8N_MANUAL_TRIGGER_TOKEN`: 수동 workflow 실행 전용
- `N8N_RISK_MANAGER_TOKEN`: RiskManager sub-workflow 전용

두 token은 서로 다른 16자 이상 값으로 유지한다. 변경 후 encrypted n8n
credential을 동기화한다.

```bash
./automation/n8n/sync-infisical-credentials.sh
```

workflow JSON에는 credential ID만 저장한다. 수동 runner는 token을 HTTP header
stdin으로 넘겨 process argument와 host 파일에 남기지 않는다. 무인증 호출은
`403`이어야 한다.

## 상태 확인

```bash
docker ps --filter name=toss-trader \
  --format '{{.Names}}\t{{.Status}}'

docker exec toss-trader-automation-1 printenv TRADING_ENABLED

docker exec n8n node -e \
  'fetch("http://toss-trader-automation:8088/healthz").then(async r=>console.log(r.status,await r.text()))'
```

기대값:

- `automation`, `metrics`, `hermes-analysis`, `alertmanager`: healthy
- automation health: HTTP `200`
- `TRADING_ENABLED`: `false`

## 장애별 판단

### Python Task Runner 경고

`Failed to start Python task runner ... Python 3 is missing`은 startup 경고다.
현재 Toss workflow는 JavaScript Code node만 사용하므로 영향 없다. Python Code
node가 필요해지면 main n8n image에 Python을 추가하지 말고 external runner
sidecar를 별도 설계한다.

### Task Broker 5679 충돌

원인: 실행 중인 n8n 컨테이너 안에서 `n8n execute`를 추가 실행함. 운영 n8n과
CLI 프로세스가 같은 `127.0.0.1:5679`를 bind한다.

조치:

1. 운영 n8n PID는 유지한다.
2. 추가 `n8n execute` PID만 종료한다.
3. 이후 수동 작업은 인증 Webhook으로 실행한다.

### RiskManager 30초 timeout

`RiskManager 정책 판단·기록`에서 `timeout of 30000ms exceeded`가 연속 발생하면
Toss PostgreSQL lock을 먼저 확인한다. runtime audit 연결에서 `CREATE INDEX`나
`ALTER TABLE`이 relation lock을 기다리면 paper subprocess와 callback 사이
교착이다.

현재 구현은 audit 전용 PostgreSQL 연결에서 schema DDL을 실행하지 않고
`automation_run_logs` INSERT만 수행한다. schema 생성·변경은 일반 초기화 경로에만
남아 있다. 재발 시 DB나 volume을 지우지 말고 대기 query와 부모 execution을 먼저
확인한다.

```bash
docker exec dgst_postgres psql -U postgres -d postgres -P pager=off -c \
  "SELECT state,wait_event_type,wait_event,left(query,120) AS query
   FROM pg_stat_activity
   WHERE datname='toss_trader'
     AND (wait_event_type='Lock' OR state='idle in transaction');"
```

### MA20/MA60 이력 부족

Toss candle 요청은 성공했지만 저장 이력이 61개보다 적으면 오류가 아니다.
종목 결과에 `skipReason`을 남기고 `failed_count`와 API 오류 streak는 증가시키지
않는다. 이력이 쌓이면 자동으로 평가 대상에 복귀한다.

### n8n workflow failure 알림

Telegram에는 원본 cycle JSON·shared snapshot·캔들 목록을 보내지 않는다. 다음처럼
짧은 운영 요약만 보낸다.

```text
n8n workflow 실패
workflow: toss-trader-daily / execution: 162
단계: rule-cycle (rule · 1d)
cycle 종료 코드: 3
종목 처리 실패: 1/17
487400 오류: <원인>
```

`exitCode=3`은 일부 종목만 실패한 partial cycle이므로 Telegram severity는 `warning`이다.
그 외 workflow 실패는 `critical`이다. 어느 경우든 후속 단계는 성공으로 위장하지 않고
실패 분기에서 종료한다. n8n execution에는 원본 응답이 남고,
`automation_run_logs.details.failure`에는 stage, 종료 코드, summary, 최대 5개 종목
오류를 구조화해 남긴다.

## 2026-08-13 마감 검증 기록

- n8n execution: `224`, `success`
- 소요 시간: 약 11.6초
- rule: 17종목, 신호 0, 체결 0, 제외 1, 실패 0
- Hermes: 17종목, 신호 0, 체결 0, 제외 1, 실패 0
- 제외 종목: `487400`, 일봉 1개로 61개 미달
- rule 일일 수익률: `+2.8425%`
- Hermes 일일 수익률: `+3.7269%`
- Hermes token: prompt 5,293 / completion 189 / total 5,482
- Telegram: accepted, Alertmanager Telegram failure counter 0
- Toss API 연속 오류: 0
- PostgreSQL lock·idle transaction: 0
- `TRADING_ENABLED=false`

이 기록은 특정 종목 매매 권고가 아니라 자동화 운영 검증 결과다.

## 배포 전후 검증

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check .

# Infisical Machine Identity 인증 후 secret을 주입해 실행
infisical run --env=prod --path=/ -- docker compose config -q
```

배포 후 RiskManager 승인·거부 왕복, automation health, n8n health, DB lock,
Alertmanager Telegram failure counter를 확인한다. 마감 리포트 중복 전송을 피하려고
배포 검증만을 위해 daily Webhook을 반복 호출하지 않는다.
