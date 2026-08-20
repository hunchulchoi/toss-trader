# Toss Trader 변경 이력

기능이 코드에 들어온 날짜다. 운영 반영은 항목에 따로 적는다.
새 항목이나 기존 항목의 추가 기록에는 `- 기록 시각: YYYY-MM-DD HH:MM KST`를
쓴다. 배포 시각이 다르면 실제 배포 시각을 별도로 쓴다.
매매 권고 아님.

## 2026-08-20

### 오후 universe 랭킹을 KRX 전일 대금으로 전환

- 기록 시각: 2026-08-20 11:38 KST
- 구멍: Toss `MARKET_TRADING_AMOUNT duration=realtime`는 당일 장중 대금이라
  전일 확정 모집단과 갈라진다. 오전 freeze 15종을 오후에 그대로 쓰면 소스
  전환이 안 된다
- 동작: 서울 12:00부터 유가 `stk_bydd_trd` + 코스닥 `ksq_bydd_trd`의 직전
  KR 영업일 `ACC_TRDVAL` 상위 100. 6자리 숫자 종목만. Toss `stocks()`로
  STOCK·보통주·정지 필터 유지. `ranking_source`로 오전 Toss cache와 분리.
  키 없음·401·빈 블록은 fail-closed, Toss 폴백 없음
- 운영: 반영함. 2026-08-20 11:38 KST `main` `ca3957c` automation 재빌드·재생성.
  health `healthy`, restart 0, `TRADING_ENABLED=false`. `KRX_API_KEY` 주입
  확인(값 미출력). 12:00 KST cycle부터 KRX 전일 대금. 오전 Toss 15종은
  `ranking_source`가 달라 재사용하지 않음

### 유니버스 멤버십에서 가격 셋업 분리

- 기록 시각: 2026-08-20 10:15 KST
- 구멍: 09:00 성공 `selected_count=0`이 서울일을 freeze해 cycle 관측이 0.
  컬렉터 장애 아님
- 동작: 완결 200봉 적격 최대 15가 membership. 가격 setup은 BUY 게이트.
  선정 0종은 freeze하지 않음
- 운영: 반영함. 2026-08-20 10:15 KST `main` `eb5aa03` 푸시 후 `automation`
  재빌드·재생성. health `healthy`, restart 0, `TRADING_ENABLED=false`.
  다음 1m(10:20)부터 0종 cache 무시하고 적격 15 재선정

### 장전 Hermes 초보자용 설명과 결측 의미 교정

- 기록 시각: 2026-08-20 09:12 KST
- 동작: 장전 결론을 먼저 말하고 RISK_OFF·NEUTRAL 등 전문용어를 쉬운 말로
  풀어 설명. 평가·승인 종목 수와 핵심 차단 사유, 데이터 상태를 4~6문장으로 요약
- 의미: `missing:*`와 `completed-daily-candles(n/200)`만 실제 데이터 부족으로
  설명. `missing-price-setup`, `flow-not-confirmed`, `event-imminent`는 각각 가격
  패턴·수급 반전 조건·임박 이벤트에 따른 정상 탈락이며 데이터 누락으로 표현 금지
- 범위: Python 직접 실행과 n8n 운영 workflow 자산에 동일 지침 반영
- 배포 시각: 2026-08-20 09:22 KST. automation 재빌드·재생성 후 healthy,
  n8n workflow import·publish·재시작. 게시 버전 active, 16 nodes/12 connections,
  초보자 설명·가격/수급 의미·장애/결측/조건 구분 문구 확인. `TRADING_ENABLED=false`

### Cycle Timeline 서울 24시·오늘 날짜

- 기록 시각: 2026-08-20 09:00 KST
- 동작: `/cycles` 시각을 `ko-KR` 12시간제 잘린 `04:07`이 아니라 서울 24시
  `16:07`로 표시. 날짜 필터 기본값은 최신 기록이 아니라 서울 달력 오늘
- 운영: 반영함. 2026-08-20 09:06 KST `main` `30b933b` 푸시 후 `timeline`만
  재빌드·재생성. health `healthy`, restart 0, `TRADING_ENABLED=false`,
  Tailscale `/healthz`·`/cycles` 200. 라이브 `/assets/cycles.js`에
  `hour12: false`·`seoulToday` 확인

## 2026-08-19

### 한국장 11:50 중간 paper 브리핑

- 기록 시각: 2026-08-19 21:19 KST
- 동작: 기존 다중분석 panel을 평일 11:50에도 실행. 예약·수동·인증 webhook 모두
  Toss 한국장 calendar를 먼저 확인하므로 휴장·calendar 장애에는 실행하지 않음
- 구분: queue context에 `midday`/`close`와 관측 시각을 저장. 중간 분석 prompt와
  Telegram은 미완결 장중 관측임을 명시하고 종가·일일 성과 확정을 금지
- 실행: Hermes no-agent poll을 11:50~11:59와 15:00~17:59 KST로 분리해
  중간 queue가 마감까지 지연되지 않도록 함
- 배포 시각: 2026-08-19 21:14 KST automation·Hermes analysis 재빌드,
  21:15 KST Hermes runner와 중간 poll cron 반영, 21:16 KST n8n import·publish·재시작
- 운영 확인: 2026-08-19 21:19 KST. automation·Hermes analysis healthy,
  n8n·main Hermes running, 전부 restart 0, `TRADING_ENABLED=false`.
  published workflow의 두 cron·세 calendar gate·error workflow 확인. panel queue 0,
  runner 빈 queue 무알림 exit 0

### 마감 paper 다중 분석 패널

- 기록 시각: 2026-08-19 18:40 KST
- 동작: 단일 Hermes 마감 분석을 GPT quant, Grok 4.6 Fast skeptic,
  Gemini 3.7 Flash Risk의 독립 분석·상호검토와 Hermes 최종 판정으로 교체.
  n8n은 cycle JSON을 DB queue에 넣고, Cursor 인증이 있는 main Hermes cron이
  read-only 모델 호출을 실행
- 감사: `daily_analysis_panels`와 `daily_analysis_opinions`에 panel 상태, 의견
  7건, 모델/provider, input/output/cache token을 idempotent 저장. 완성된 Hermes
  판정만 기존 Alertmanager→Telegram 경로로 전송
- 보안: n8n·terminal child에 Docker socket/Cursor credential/임의 shell 권한을
  추가하지 않음. `TRADING_ENABLED=false` 유지
- 배포 시각: 2026-08-19 19:56 KST automation 재빌드·재생성,
  19:57 KST PostgreSQL panel schema 초기화, 19:59 KST n8n import·publish·restart
- 운영 확인: 2026-08-19 20:04 KST. automation healthy/restart 0,
  n8n active/restart 0, `TRADING_ENABLED=false`. Hermes persistent runner와
  UTC `* 6-8 * * 1-5`(KST 15:00~17:59) no-agent cron 설치. queue 0 상태
  dry smoke는 모델·Telegram 호출 없이 exit 0
- 추가 기록: 2026-08-19 19:39 KST. main Hermes에서 GPT·Grok 4.6 Fast·
  Gemini 3.7 Flash의 실제 read-only JSON/token 응답 성공. Cursor CLI의 동시
  프로세스 경합을 피하도록 각 round 내부 호출은 순차 실행하고, round 사이의
  독립 분석→상호검토 경계는 유지
- 추가 기록: 2026-08-19 20:11 KST. 오늘 Rule/Hermes 마감 cycle을 test
  execution으로 다시 생성해 전체 panel을 수동 실행. 약 3분 18초에 7단계 완료,
  panel `succeeded`, 의견 7건, provider-reported total token 268,376을 DB에서
  확인. Hermes 최종 판정은 Alertmanager가 수락했고 runner exit 0

### Hermes 종목 판단에 cycle 시세·수급 스냅샷

- 기록 시각: 2026-08-19 17:25 KST
- 동작: advisor user JSON에 최근 완결 일봉 30·분봉 60·setup-v2·PIT 수급 요약.
  sidecar tool/Toss 직접 조회 없음. 한도 숫자만으로 승인하지 말라고 prompt 보강
- 운영: 반영함. 2026-08-19 17:34 KST `automation` recreate. health `healthy`,
  restart 0, `TRADING_ENABLED=false`. 라이브 이미지에 market prompt 확인

### OpenDART 당일 coverage 조기 고정 방지

- 기록 시각: 2026-08-19 17:20 KST
- 원인: 당일 00:01 수집 4건을 `SUCCESS`로 고정해 이후 329건으로 증가한
  OpenDART 접수를 재조회하지 않음
- 동작: 당일은 18:30에 갱신만 하고 checkpoint하지 않음. 다음 날 00:10에
  전일을 다시 전량 조회한 뒤에만 완료 고정하며, 같은 날 생성된 기존 조기
  checkpoint는 무시. 이벤트의 다음 세션 계산에는 실행 당일도 포함
- 검증: 조기 checkpoint 재조회, 당일 반복 갱신, 00:10/18:30 이중 스케줄
  회귀 테스트 포함 전체 332 테스트와 Ruff 통과
- 배포 시각: 2026-08-19 17:28 KST
- 운영: `main` `977f928`로 pit-collector만 재빌드·재생성. restart 0,
  `event_rows=742`, KIS 실패 0. 8월 19일 DB 공시는 4건에서 345건으로
  복구했고, 이후 추가된 당일 공시는 18:30 갱신·00:10 최종화 대상으로 유지

### 공식 프로젝트 용어집(Glossary) 구축

- 기록 시각: 2026-08-19 17:15 KST
- 동작: 시스템 아키텍처·안전, PIT 무결성, setup-v2.2 전략 규칙, 원장/손익 엔진,
  자동화·연동, 멀티에이전트 체계 용어를 총망라한 [`docs/glossary.md`](glossary.md)
  작성 및 주요 문서 연결
- 운영: 문서만, 배포 없음

### 1d 마감 일봉 cycle의 intraday review DB 연결 타이밍 수정

- 기록 시각: 2026-08-19 16:08 KST
- 동작: `_run_paper_cycle`에서 `interval=1d` 시 수행하는 `_intraday_review_for_day` 호출을
  `cycle_state` 저장소 close 이전(`ExitStack` 내부)으로 이동하여 `OperationalError: the connection is closed` 해결
- 운영: 반영함. 2026-08-19 16:05 KST `automation` 컨테이너 재빌드·재기동 완료

### Hermes 대화 조회 페이지

- 기록 시각: 2026-08-19 16:45 KST
- 동작: `/hermes`가 `automation_run_logs`의 `hermes_trade`·`market_scan`·`daily`를
  읽는다. 종목 판단은 기존 `rationale`. 장전/마감은 이제 `details.assistant`에
  응답 본문(최대 4000자). 요청 JSON·secret은 계속 안 넣음. 과거 장전/마감은
  token만 있어 본문 없음으로 표시
- 운영: 반영함. 2026-08-19 17:05 KST `timeline` recreate. `/healthz` 200,
  restart 0, healthy. `/hermes` 200. API `hermesConversations` 58건

### KIS 최신 세션 원장 지연 보완

- 기록 시각: 2026-08-19 16:26 KST
- 원인: KIS 응답은 8월 19일까지 정상인데 `session_index`를 DataGo 시세
  원장에만 의존해 원장 최신일인 8월 14일 이후 행을 폐기
- 동작: DataGo 마지막 세션 이후 완료일까지 Toss KR 캘린더가 확인한 영업일만
  연속 인덱스로 확장. 휴장일은 계속 제외하며 KRX 수동 import도 같은 계산 사용
- 검증: 전체 328 테스트, Ruff, 실제 KIS/Toss 격리 smoke. 임시 DB에
  8월 14·18·19일을 인덱스 1·2·3으로 저장했고 실패 0,
  `TRADING_ENABLED=false` 확인
- 추가 기록: 2026-08-19 16:36 KST. 신규 상장 보통주의 영숫자 6자리
  단축코드를 허용하고, 금액이 빈 과거 행은 제외하며 비정상 숫자는 종목 단위로
  격리. KIS 실조회에서 엔비알모션 `0004V0` 30행·최신 8월 19일 확인
- 배포 시각: 2026-08-19 16:40 KST
- 운영: `main` `72cece4`까지 푸시하고 pit-collector만 재빌드·재생성.
  restart 0, `flow_rows=253`, `AVAILABLE_FIRST_OBSERVED`, 실패 0. PostgreSQL에
  KIS 8월 18일 328종·19일 329종을 확인했고 `0004V0`도 두 세션 모두 저장

### setup-v2 universe membership 강화

- 기록 시각: 2026-08-19 15:15 KST
- 동작: 거래대금 최대 100개를 정적 적격 보통주 Top 30으로 재랭크하고
  `TOP_GAINERS` 선정 영향을 제거. 가변 계좌 Risk는 BUY 실행 단계에만 유지
- 장애 계약: 랭킹·metadata·가격 데이터 오류는 성공 0종과 분리해 실패·재시도.
  정상 이력 부족·가격 setup 불일치만 정상 탈락 및 0종 cache 허용
- 추적: `dynamic_universe_decisions.eligible_rank`를 idempotent migration으로
  추가. raw amount rank와 함께 순서 감사 provenance로 보존. metadata/config/
  candle snapshot이 없으므로 exact replay는 아직 지원하지 않음
- Risk 계약: universe 정적 규칙의 Python/n8n policy v2 parity를 맞추고,
  기존 BUY 실행 차단 규칙은 유지
- 추가 기록: 2026-08-19 15:44 KST. 최대 100회의 순차 n8n 호출을 피하도록
  universe 정적 membership은 로컬 검증으로 고정. 실제 BUY·SELL만 n8n Risk를
  사용하며 trade는 기존 policy v1, 선택적 universe 호환 계약은 v2로 분리
- 배포 시각: 2026-08-19 16:04 KST
- 운영: `main` `bea6203`까지 푸시하고 automation을 재빌드·재생성. health
  `healthy`, restart 0, `TRADING_ENABLED=false`; 실행 이미지에서 로컬 universe
  Risk와 원격 trade Risk 경계를 확인. PostgreSQL `eligible_rank` migration 및
  컬럼 확인 완료. 당일 기존 성공 cache는 유지해 새 선정은 다음 서울 거래일 적용

### Cycle 카드 메트릭·사유 한 줄

- 기록 시각: 2026-08-19 14:56 KST
- 동작: 종목/신호/체결/실패를 한 줄. 신호 mint·체결 amber·실패 red는 0 초과만.
  idle 사유와 퍼널 건수를 details summary 한 줄에 합침
- 운영: 반영함. 2026-08-19 16:22 KST `timeline` recreate. `/healthz` 200,
  restart 0, healthy. 라이브 `/assets/cycles.js`에 `cycle-funnel-n` 확인

### Cycle/장부 타임라인 compact UI

- 기록 시각: 2026-08-19 14:40 KST
- 동작: 실행흐름 row·KPI·히어로 높이 축소. 배경 그리드·ambient 제거.
  universe 스파크라인 28px
- 운영: 반영함. 2026-08-19 14:50 KST `timeline` recreate. `/healthz` 200,
  restart 0, healthy. Tailscale `100.74.208.69:19094` `/cycles` 200

### 동적 universe vs setup-v2 교차토론

- 기록 시각: 2026-08-19 13:55 KST
- 동작: agy 수정채택. 08:30 freeze·눌림-only 철회. 합의는
  [`docs/2026-08-19/universe-strategy-debate.md`](2026-08-19/universe-strategy-debate.md)
- 운영: 문서만. 장중 코드·배포 없음

### setup-v2 가격 후보 기반 장중 universe

- 기록 시각: 2026-08-19 13:48 KST
- 동작: 거래대금·상승률 랭킹 종목 중 직전 완결 일봉 200개의 pullback 또는
  oversold reversal 통과 종목만 RiskManager 후보로 사용. 최대 15종을 서울
  거래일 동안 고정하며 부족분을 급등 종목으로 채우지 않음. 0종도 정상 cycle
- 추적: 기존 보유 종목은 선정 밖이어도 SELL 경로 유지. 후보별 가격 setup
  부족·평가 실패 사유를 `dynamic_universe_decisions`에 기록
- 배포 시각: 2026-08-19 13:51 KST
- 운영: `main` `5bf0f24`로 `automation`만 재빌드·재생성. health `healthy`,
  restart 0, `TRADING_ENABLED=false`, `/healthz` 200. 13:55 Rule/Hermes 1m은
  둘 다 succeeded, 실패·API 오류 0. 당일 기존 성공 universe cache는 보존해
  새 가격 필터의 첫 운영 선정은 다음 서울 거래일에 실행

### changelog 시각 기록 규칙

- 기록 시각: 2026-08-19 13:30 KST
- 동작: 이후 changelog 작성·추가 시 실제 사건 또는 관측 시각을 KST로 기록
- 운영: 프로젝트 규칙과 문서 형식만 변경. 서비스·DB·매매 상태 변경 없음

### paper 타임라인 빈 장부 기동

새 Rule/Hermes 세대는 체결 0인데 타임라인이 fills를 요구해 컨테이너가 죽고
Tailscale 19094가 502였다.

- 동작: 체결 없으면 오늘(또는 cycle 날짜) 초기현금 하루. 페이지 유지
- 운영: 09:49 KST `9a0812e` `timeline` 재빌드·재기동. health `healthy`,
  restart 0, `TRADING_ENABLED=false`, `/healthz` 200, days 1.

### 1m v2 일봉 미달을 skip으로 유지

Hermes shared snapshot이 v2 후보를 다시 만들 때 완결 일봉 200 미달을
종목 오류로 올려 n8n warning이 반복됐다. 장중 1m는 동적 유니버스 일봉을
안 넣어서 0/60행이 나왔다.

- 동작: 재생성도 `setup-v2:missing:`은 skip. 1m v2 prepare가 일봉 200 수집
- 운영: 09:31 KST `e9898e5` `automation` 재빌드·재기동. health `healthy`,
  restart 0, `TRADING_ENABLED=false`, healthz 200. n8n daily webhook은 안 침.

## 2026-08-18

### 마감 리포트 당일 1m 퍼널

15:40 마감 Hermes가 1d cycle의 `no-crossover`만 읽고 장중 v2 판정을
놓치던 구멍을 막았다. 같은 서울 일자 1m `cycle_insight`를 모아
체결·마지막 사유를 `intradayReview`로 넘긴다. v2 무신호 idle은
`no-crossover`가 아니라 `v2-idle`이다.

- 동작 변화: 마감 JSON `dailyReview`, 텔레그램 당일 1m 한두 줄
- 운영: 코드만, 배포 대기

### Rule/Hermes paper 실험 세대 전환

15:40 마감 workflow 성공 후 기존 Rule/Hermes 자료를 삭제하지 않고
`rule-v1-20260818`·`hermes-v1-20260818`로 archive했다. 2026-08-19 시작
활성 계정은 각각 초기현금 1,000,000원, 체결·보유 0으로 생성했다. 전환 전
복구 dump를 만들었고 paper MCP 및 DB 불변식을 재검증했다.

운영: `TRADING_ENABLED=false` 유지. KIS 수급 6세션 미충족 시 BUY 0건 정상.

### 장전 분석 setup-v2.2 정렬

08:30 시장 스캔의 종목 후보를 MA20/60·20일 모멘텀 랭킹에서 setup-v2.2로
교체했다. 시장 레짐은 참고 정보로 유지하되, 후보는 200개 완결 일봉의
눌림/RSI 반전과 PIT 수급·이벤트를 모두 평가한다. 보고서는 승인 후보뿐 아니라
스캔·평가·차단 수와 상위 차단 사유를 표시한다. Hermes도 MA 점수를 새 후보
근거로 만들지 않고 v2 셋업·수급·데이터 누락만 해석한다.

### setup-v2.2 독립 진입·청산 상태기계

setup-v2가 MA 신호를 뒤에서 거르던 구조를 제거했다. 전일 완결 일봉이 직접
후보를 만들고 다음 세션 첫 완결 1분봉 시가에서 갭을 재검사한 뒤, 실제
equity·cash·open/cluster heat로 정수 수량을 계산한다. 0주는 1주로 올리지 않는다.

- 구조손절: 신호 일봉 저가와 ATR14 1.5배 중 넓은 거리
- paper 마찰: 진입·청산 각각 5bps 불리 적용
- 청산: 완결 1분봉 저가가 stop을 터치하면 다음 완결봉 시가
- 눌림 무효: 완결 일봉 종가가 사전등록 MA50 아래
- 영속: 포트폴리오별 setup, stop, planned heat, cluster, pending exit
- 동시 후보: fill 전에도 heat·cash를 예약해 같은 cycle 중복 사용 차단
- 이벤트: 날짜 없는 사전예고는 실제 blocking 공시 전까지 신규진입 차단
- 기존 MA 포지션: 가짜 stop을 소급 생성하지 않고 legacy-unmanaged로 격리

Cursor 1차안의 RSI70·10일 청산은 근거 없는 휴리스틱이라 철회했다. 1분 MA
BUY/SELL과 time exit은 v2.2 실행 경로에 없다. 281개 회귀테스트와 Cursor
재검토를 통과했다.

운영: 13:22 KST `4bdf516`까지 푸시하고 `automation`을 재빌드·재기동했다.
health `healthy`, restart count 0, v2.2 모듈 로드를 확인했다. KIS 수급
6세션 전에는 BUY 0이 정상이며, `TRADING_ENABLED=false` 유지.

배포 직후 기존 MA 보유분에 v2 plan이 없어 n8n cycle이 20/20 실패했다.
구형 포지션은 자동청산하지 않고, 보유 중에는 포트폴리오 신규 BUY를 차단한다.
이 상태는 데이터 오류가 아닌 정상 `setupV2Blocked`로 기록한다.
Hermes shared snapshot 재생도 legacy 차단을 후보 재생성보다 먼저 적용한다.

### 타임라인 종목 Toss 링크

보유·체결·비교·판단·오류·1분봉 화면의 국내 6자리 종목을 클릭하면
`https://www.tossinvest.com/stocks/A{종목코드}/order`로 이동한다. 해외 코드와
비종목 값은 링크로 만들지 않는다.

### KIS 종목별 수급 first-observed PIT

종목별 외인/기관 수급 공식 소스가 없어 setup-v2 BUY가 fail-closed였다.
한국투자증권 Open API `FHPTJ04160001`로 완료 세션만 저장하고, 첫 관측
`available_at`은 `INSERT OR IGNORE`로 고정한다.

- 동작: `collect-kis-flow`, pit-collector 15:40 KST 이후 당일 포함
- 운영: 11:54 KST `pit-collector` 재빌드·재기동 완료. Infisical prod의
  `KIS_APP_KEY`/`KIS_APP_SECRET`이 주입됐고 `TRADING_ENABLED=false`를 확인했다.
  Docker 수집 DB에는 검증된 로컬 `market_symbols` 200종목만 멱등 동기화했다.
- 운영: 12:03 KST `automation`도 재빌드·재기동했다. 실행 이미지에서
  setup-v2 게이트와 `setupV2Blocked` funnel을 확인했고 health는 정상이다.
  수급 0행 상태이므로 현재 신규 BUY는 fail-closed다.

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
