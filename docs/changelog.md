# Toss Trader 변경 이력

기능이 코드에 들어온 날짜다. 운영 반영은 항목에 따로 적는다.
매매 권고 아님.

## 2026-08-18

### KIS 종목별 수급 first-observed PIT

종목별 외인/기관 수급 공식 소스가 없어 setup-v2 BUY가 fail-closed였다.
한국투자증권 Open API `FHPTJ04160001`로 완료 세션만 저장하고, 첫 관측
`available_at`은 `INSERT OR IGNORE`로 고정한다.

- 동작: `collect-kis-flow`, pit-collector 18:00 KST 이후 당일 포함
- 운영: 코드만, 배포 대기. Infisical `KIS_APP_KEY`/`KIS_APP_SECRET` 필요.
  키 없으면 pit-collector 기동 실패

## 2026-08-14

### Agent 성과 리뷰 교차토론 기록

Cursor가 성과·운영을, agy가 시장·전략을 검토하고 서로 반론했다. agy의
외부시장 수치·URL은 독립 검증 실패로 철회했으며, 날짜 폴더에 초안·반론·최종
결정을 보존했다. 장중 파라미터 변경 금지와 한 거래일 한 변수 원칙에 합의했다.

운영: 문서만 반영. 전략·리스크 값·DB·서비스 변경 없음.

### Infisical machine token 비출력 규칙

universal-auth 로그인 성공 시 CLI가 access token을 stdout에 출력할 수 있었다.
machine identity 값은 `.env`에서 읽되 로그인 출력은 메모리 변수로만 캡처하고,
노출된 token은 폐기·회전 전까지 사용하지 않도록 프로젝트 규칙을 강화했다.

운영: 규칙만 반영. Infisical secret·DB·서비스 변경 없음.

### DB 접속정보 Infisical 단일 원천

agent가 실행 중인 container 환경에서 DB 접속정보를 유추할 수 있었다. 프로젝트
규칙에 환경별 DB 접속정보는 Infisical에서만 주입하고, 인증 불가 시 다른 원천으로
우회하지 않도록 명시했다.

운영: 규칙만 반영. secret·DB·서비스 변경 없음.

### Herdr 역할별 worktree 운영

Codex·Cursor·agy가 같은 checkout을 공유해 변경 경계가 흐려졌다. 루트
`AGENTS.md`, 중앙 작업판, 결정 기록을 추가하고 역할별 branch/worktree를
표준으로 정했다.

운영: 개발 환경 구성만. 거래·n8n 서비스 변경 없음.

### 장중 특이사항 Telegram JSON·슬롯 거부 도배

성공한 1분 사이클이 `특이사항 Telegram`에서 `JSON parameter needs to be
valid JSON`으로 매 5분 critical이 났다. jsonBody `{ ...$json }` spread가
원인이다. `rule`/`hermes`를 노드 이름으로 명시한다.

슬롯이 꽉 찬 뒤 continuation BUY가 `max-open-positions`를 매 사이클
찍어 Telegram이 거부 목록으로 도배됐다. 이 코드만 특이사항에서 뺀다.
RiskManager 판단·audit 기록은 그대로다.

운영: 2026-08-14 15:09 KST automation 재빌드. n8n `toss-trader-intraday-paper`
import 후 재활성화·n8n 재시작. live nodes spread 없음. 15:15 정규 실행
`success`. `TRADING_ENABLED=false`.

### paper 포지션 슬롯 확대·손실 시 청산 허용

- 최대 동시 보유 종목을 5종에서 10종으로 확대
- `daily-loss-limit`은 신규 `BUY`만 차단
- 일일 손실률이 -3% 이하라도 보유 포지션 `SELL`은 허용

### 무신호 원인 (`idleReason`)

공용 Hermes가 `toss_paper_status`로 신호 수·체결만 보고 현금 대기를
자금 부족으로 오해하던 구멍을 막았다.

- `paper_cycle_runs.cycle_insight` JSON 저장
- 사이클 퍼널: scanned / evaluated / noCrossover / sellNoPosition /
  alreadyHeld / signals / riskRejected / fills
- 종목별 `symbolStates`: close, MA short/long, above|below
- status에 조회 시점 `cash`, `cashWeight`, `openPositionCount`
- 데드크로스인데 보유 없으면 `sell-no-position` (예전엔 신호만 버리고 끝)
- SOUL: `signals=0`이면 현금 탓하지 말고 `idleReason`을 읽는다

운영: 같은 날 오전 automation·paper-mcp 재빌드. 수동 1분 사이클 확인.
19종 스캔, 신호 0, `idleReason=no-crossover`. 삼성전자 1분 MA short < long.

상세: [`paper-mcp.md`](paper-mcp.md).

### 1분 trend continuation

1분봉 신규 골든크로스만 사면 이미 상승 정렬인 종목은 현금으로 남는다.
장중 1분 매수에 일봉 필터를 붙였다. 슬롯 5·수량 1주는 그대로다.

진입 (교차가 없을 때):

1. 일봉 `close > MA20 > MA60` 이고 20일 모멘텀 > 0 (`RISK_ON`)
2. 1분 `close > MA20 > MA60`
3. 그 종목을 아직 안 들고 있음
4. 종목당 한국 날짜 기준 하루 1회 (`…-cont-YYYY-MM-DD`)

이미 보유면 신호 폐기, `already-held`. 5종 한도면 신호는 나고
`max-open-positions`로 거부된다. 일봉 60개가 없으면 저장된 일봉을 쓰고,
모자라면 그때만 일봉 60개를 수집한다. 마감 일봉 사이클에는 이 경로 없음.

운영: 2026-08-14 10:22 KST automation·paper-mcp 재빌드. SOUL 반영 후
공용 hermes 재시작. 수동 1분 사이클 rule·hermes 각 19종, 신호 0, 체결 0.
`idleReason=no-crossover` (already-held 1, no-crossover 18). MA above 7 /
below 11 / equal 1. `TRADING_ENABLED=false`. paper-mcp healthz tools=3.
