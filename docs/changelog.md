# Toss Trader 변경 이력

기능이 코드에 들어온 날짜다. 운영 반영은 항목에 따로 적는다.
새 항목이나 기존 항목의 추가 기록에는 `- 기록 시각: YYYY-MM-DD HH:MM KST`를
쓴다. 배포 시각이 다르면 실제 배포 시각을 별도로 쓴다.
매매 권고 아님.

## 2026-08-25

### Rule 현재 완결봉 실행가와 invalid-stop 회복 shadow

- 기록 시각: 2026-08-25 17:29 KST
- 실행가: 첫 완결봉 시가는 D+1 gap·setup-low 훼손 판정에만 유지한다. 실제
  Rule/Hermes v2.3 paper 진입·수량·stop은 해당 cycle 최신 완결 1분봉 종가와
  5bp 불리한 slippage로 계산한다
- stale 차단: 현재 분 완결봉이 없으면 `setup-v2:waiting:current-bar`로 대기하며
  09:01 시가를 09:05 이후 체결가로 소급 재사용하지 않는다
- 시각 계약: Toss `09:01` 분봉은 09:00~09:01 완결봉이므로 완료 판정에서
  추가 1분을 더하던 지연을 제거했다
- 연구: 시초가가 전일 setup low 이하라 Rule 진입이 무효화된 종목은 09:15 이후
  setup low 회복 뒤 다음 3봉이 99.5% 이상을 유지하면
  `setup-v2:shadow:invalid-stop-reclaim`으로만 기록한다. signal·Risk·fill 없음
- 안전: Rule 거래당 0.5%, 전체 heat 2%, UNKNOWN cluster 1%와 모든 계좌 Risk
  유지
- 8월 25일 read-only 반사실: 실제 Rule 2건은 현재 완결봉 기준 체결가가 각각
  `4762.3800→4752.3750`, `8764.3800→8574.2850`으로 교정된다. invalid-stop
  2종은 09:30까지 회복 shadow 조건도 통과하지 않아 강제 매수는 늘지 않는다
- 검증: 414 unit tests, changed-file Ruff, Git whitespace 통과
- 배포: 미배포

### Hunter 이중 LLM 승인 제거와 유동성 수량 제한

- 기록 시각: 2026-08-25 16:57 KST
- 역할 분리: batched Hunter `approve`를 방향 판단으로 유지하고, 이후 Trade
  Hermes 응답은 Hunter 신호에서 의견·veto code·token 감사만 남긴다. 거부나
  분석 장애가 Hunter paper 주문을 막지 않으며 일반 Hermes v2의 fail-closed는
  그대로다
- 유동성: 10:01~10:05 최신 완결 5개 분봉의 평균 분당 거래대금 중 10%까지만
  Hunter 주문에 사용한다. 기존 risk/cash/heat/70만원 한도를 추가로 적용하며
  1주 미만이면 거부한다
- 재검증: 완결 5분봉 누락, 재돌파 가격 이탈, 직전 5분 대비 거래대금 50% 미만,
  stop 훼손·목표 도달·stop 거리 3% 초과를 코드가 차단한다
- 설명력: 최종 Hermes payload에 최근/직전 평균 거래대금, 가속, 주문 참여율을
  넣고 허용된 veto code와 수치 evidence만 요구한다
- 검증: 411 unit tests, changed-file Ruff, Git whitespace 통과
- 배포: 미배포

### 09:30 경계 복구와 Hermes Hunter paper 진입

- 기록 시각: 2026-08-25 16:19 KST
- 경계 수정: cycle 시작이 `09:30:00.xxx`여도 09:30분 전체를 기존 v2.3
  진입창으로 인정한다. 09:31부터는 기존처럼 shadow-only다
- 표본 수정: 10:00 Hunter 첫 평가 직전 known research pool 전체의 당일 1분봉을
  한 번 보강한다. 성공 감사행이 있으면 같은 날 전수 재수집하지 않는다
- paper 진입: deterministic Hunter 상위 2개 중 Hunter Hermes가 `approve`한 종목만
  10:01~10:05 Hermes portfolio 후보로 승격한다. 당시 최신 완결 1분봉으로 다시
  가격을 잡고, 목표가 도달·stop 훼손·stop 거리 3% 초과를 차단한 뒤 Hermes
  trade advisor와 기존 RiskManager를 모두 통과해야 paper fill을 만든다
- 분리: Rule과 09:00 setup-v2.3 계약은 바꾸지 않는다. 기존
  `momentum-shadow-v2` 감사행도 연구 결과 그대로 보존하고, 공유 snapshot의
  `hunterEntry`만 `paperOnly=true`, `strategyInput=true`로 명시해 Hermes에 전달한다
- 검증: 408 unit tests, changed-file Ruff, Git whitespace 통과
- 한계: Hunter 실제 paper 성과 근거는 아직 없고, 목표가는 후보 늦은 진입 차단에
  사용한다. 진입 뒤 청산은 기존 hard-stop/장부 mark-to-market 경로를 사용한다
- 배포: 미배포

## 2026-08-24

### Hunter shadow Hermes 검토·가상 수익률 timeline

- 기록 시각: 2026-08-24 17:31 KST
- 동작: Hunter 상위 2개를 Hermes가 한 번에 `approve/watch/reject`로 검토하고
  의견과 input/output/total token을 `momentum-shadow-advice` 감사 로그에 저장
- 성과: 10:00 가상 진입 뒤 stop 우선, 1.5R 목표, 미도달 시 마지막 저장 1분봉으로
  수익률·R배수·최대 유리/불리 변동을 계산. timeline에서 전체 Hunter와 Hermes
  승인군 평균, 종목별 계획·판정·성과를 표시
- 시점 교정: 판단 직전 09:59 완결봉까지만 신호에 사용하고 10:01 표기 봉의
  시가(10:00 이후 첫 실행 가능 가격)를 진입가로 써 v1의 시점 편향을 제거.
  규칙 버전은 `momentum-shadow-v2`
- 안전: 실제 signal·RiskManager·paper fill·주문에는 연결하지 않으며 Hermes 실패도
  실제 cycle을 막지 않음
- 배포 시각: 2026-08-24 17:37 KST
- 운영: `automation`·`timeline` 이미지를 재빌드·교체했다. 두 서비스 healthy,
  `momentum-shadow-v2`·Hermes review 함수·Hunter timeline 패널 로드,
  `TRADING_ENABLED=false`를 확인했다

### 장중 눌림 재돌파 Hunter 비매매 shadow 표본

- 기록 시각: 2026-08-24 17:05 KST
- 연구: 8월 18~21일 현재 정적 적격 1,364종목일 중 일중 +3% 가능 571종목일의
  1분봉을 복원했다. 단순 추격 24조합은 모두 손실. 3봉 유지·시장 ETF proxy
  동조·1.5R 익절 적용 시 최고 `-0.07%`, 4거래 1승 3패로 손실은 줄었지만
  양수 성과와 통계적 유효성은 확인하지 못했다
- 동작: 09:00~10:00 정적·일봉 이력 적격 `TOP_GAINERS` 최대 30종을 기존
  Top30 표본에 비차단으로 합쳐 1분봉을 수집한다. 10:00에 시초 +8% 추격 제외,
  +3% 상승→1~4% 눌림→재돌파→3봉 유지, 시장 동조, stop 거리 3% 상한,
  1.5R 목표를 적용하고 상위 2개 계획을 `automation_run_logs`에 하루 한 번 저장한다
- 안전: `strategyInput=false`, `shadowOnly=true`. Rule·Hermes universe, advisor,
  RiskManager, 주문·체결에 연결하지 않는다. 연구 ranking/수집 실패는 매매 cycle을
  실패시키지 않는다
- 배포 시각: 2026-08-24 17:18 KST
- 운영: `automation` 이미지를 재빌드·교체했다. health `ok`,
  `momentum-shadow-v1` 로드, `TRADING_ENABLED=false`를 확인했다. 월요일 forward
  표본부터 검증 예정

### 현재 process를 과거 확정 데이터에 적용한 반사실 표본

- 기록 시각: 2026-08-24 14:10 KST
- 복원: 8월 18일 12종 4,750봉, 19일 12종 4,425봉, 24일 추가
  2종 738봉을 백필했다. 합계 9,913봉, 실패 0, 전부 `session-open-reached`
- 조건: current static metadata와 D-1 DataGo 전종목 거래대금, 최종 KRX 수급·
  DART 이벤트에 현재 Rule strict 선발 및 Hermes 참고형 Top30, 거래당 2%,
  UNKNOWN heat 6%, 현금 100만원, 주문 70만원, 09:01 갭·stop 규칙을 적용
- 결과: 8월 18·19·20·21·24일 deterministic pre-advisor 신호가 각각
  5·3·4·4·5건으로 총 21건. 당일 마지막 값 상승 7건, 단순 시가 대비 평균
  -2.43%. 5거래일 Hermes pool 30종의 분봉 공백 0
- 한계: `counterfactual-final-data`. 당시 process/PIT 재현이 아니며 현재 metadata
  survivorship bias가 있다. Hermes LLM 승인, 실제 v2 exit, 수수료·shared portfolio
  PnL을 적용한 성과 백테스트가 아니다

### Toss Open API 일시적 DNS 질의 실패 및 네트워크 오류 1회 재시도 적용

- 기록 시각: 2026-08-24 14:01 KST
- 배포 시각: 2026-08-24 14:00 KST
- 구멍: `UrllibTransport`가 일시적 DNS 해석 실패(`URLError: [Errno -2] Name or service not known`)나
  소켓 타임아웃 발생 시 즉시 실패하여 장중 사이클이 부분 실패(`exitCode: 3`)로 종료됨
- 동작: `UrllibTransport`에 일시적 네트워크/DNS 오류(`URLError`, `TimeoutError`,
  `OSError`) 발생 시 0.5초 대기 후 1회 자동 재시도(`max_retries=1`) 로직 추가
- 배포: `automation` 컨테이너 재빌드 및 배포 완료

### 휴장 18:30 KIS 수급 알림

- 기록 시각: 2026-08-24 13:05 KST
- 구멍: pit-collector가 달력 없이 매일 18:30에 KIS를 쳐, 휴장·주말 제공자
  오류가 `TossTraderKisFlowFailure`로 갔다
- 동작: 당일이 KR 정규장이 아니면 KIS 생략. OpenDART는 그대로
- 배포 시각: 2026-08-24 13:12 KST pit-collector 재빌드·재생성
- 운영: 반영함. restart 0, `TRADING_ENABLED=false`. 기동 직후
  `WAITING_FOR_KIS_1540`(정규장·15:40 전). 휴장 스킵은 다음 비영업일 18:30부터
  적용

### 휴장일에 TossTraderCycleStale이 울리던 문제

- 기록 시각: 2026-08-24 13:00 KST
- 구멍: 알림이 마지막 완료 cycle 시각만 봐서, n8n이 안 도는 휴장·주말도
  25시간 stale로 취급했다
- 동작: metrics가 Toss KR calendar로 `toss_trader_kr_intraday_cycle_expected`를
  내고, 정규장 중에만 CycleStale을 평가한다. calendar 실패는 fail-open
- 배포 시각: 2026-08-24 13:12 KST metrics·prometheus 재빌드·재생성
- 운영: 반영함. metrics healthy, `kr_calendar_ok=1`,
  `kr_intraday_cycle_expected=1`(장중), CycleStale `inactive`.
  `TRADING_ENABLED=false`

### 중간 패널이 벤치마크 결측·사이징 맥락 없이 구멍만 말하던 문제

- 기록 시각: 2026-08-24 12:30 KST
- 구멍: KODEX 1분봉을 유니버스 밖에서 안 모아 `missing-1m`이 됐고, 진입창
  시각·`below-one-lot` 사이징·freeze `runId=null`을 JSON이 설명하지 않아
  패널이 해석 불가만 반복했다
- 동작: 1m cycle이 벤치마크 200봉을 같이 모으고, 1d가 결측 1분봉을 채운 뒤
  `entryWindow`·`cacheMeaning`·`reasonPath`·`armRejectDetail`을 넘긴다
- 배포 시각: 2026-08-24 12:52 KST automation 재빌드·기동
- 운영 검증: 2026-08-24 12:55 KST cycle에서 Rule 2종과 Hermes·검증용
  후보 30종을 분리해 처리했고, 후보 30종과 벤치마크 2종의 1분봉이 저장됐다.
  두 portfolio 모두 `succeeded`, 주문·체결 0, `TRADING_ENABLED=false` 확인
- 복원 시각: 2026-08-24 13:03 KST
- 복원 검증: 현재 검증 풀 30종의 오전 공백 7,058봉을 추가했고 실패 0,
  전 종목 `session-open-reached`. 13:04 KST 기준 30종 모두 정규장
  09:01~13:04 구간 244봉, 당일 전체 32종 9,050봉 확인

### 중간·마감 패널에 저장된 시세 대비를 붙임

- 기록 시각: 2026-08-24 11:20 KST
- 구멍: 11:50/15:40 패널이 스킵 사유 JSON만 보고 당일 KODEX·감시종목 움직임을
  안 봐서, 바뀐 사실이 없으면 전략 토론만 반복했다
- 동작: 1d cycle이 저장된 1분봉으로 `marketContext`를 만들고, 패널 JSON과
  프롬프트가 스킵 사유를 vsOpen/vsPrevClose와 대조하게 한다. 뉴스·사후 매수
  기회는 만들지 않는다
- 운영: 반영함. 2026-08-24 11:41 KST automation 재빌드·기동, Hermes
  `/opt/data/scripts/toss-trader-daily-panel.py` 복사. `TRADING_ENABLED=false`,
  `/healthz` ok. 다음 11:50/15:40 브리핑부터 적용

### Hermes experimental 수량 위험 예산 분리

- 기록 시각: 2026-08-24 12:37 KST
- Rule: 거래당 0.5%, 전체 open heat 2%, UNKNOWN cluster heat 1% 유지
- Hermes: paper experimental만 거래당 2%, 전체 open heat 6%, 단일 UNKNOWN
  cluster heat 6% 적용
- 공통 안전: ATR14 ×1.5 stop, 정수주 내림, 주문 700,000원, 가용 현금,
  일일 손실·API·시장시간·09:30·RiskManager 차단 유지
- 한계: 100만원 장부에서 1주 예상손실 20,000원을 넘는 종목은 Hermes도
  `below-one-lot`; 실거래·성과 개선 근거 아님

### Hermes를 v2.3 참고형 독립 paper 전략으로 분리

- 기록 시각: 2026-08-24 12:20 KST
- 후보: Rule 선택종목을 우선 보존하고 D-1 유동성 순 정적 적격 보통주로
  Hermes 관찰 풀을 최대 30종까지 구성
- 전략: Rule은 strict setup-v2.3 유지. Hermes는 `missing-price-setup`,
  `rsi-chase`, `falling-knife`를 advisor 참고 근거로 사용
- 안전: PIT·이벤트 결손, 임박 이벤트, 갭, 정수주 수량, 0.5% 위험, heat,
  현금·70만원 주문 상한, 계좌 Risk, 장 시간, 09:30 진입 제한은 하드 차단 유지
- 감사: 모든 Hermes 진입은 `hermes-experimental` signal·position setup으로
  기록. Rule과 후보·전략 계약이 달라 직접 A/B·성과 개선 근거로 사용 금지

### Paper entry 관찰창과 주문 상한 조정

- 기록 시각: 2026-08-24 11:18 KST
- 동작: paper 초기 자본 1,000,000원과 거래당 위험 0.5%, ATR stop 등 기존
  setup-v2.3 게이트는 유지하고 주문당 최대 금액만 300,000원에서 700,000원으로 조정
- 관찰: 실제 BUY 진입창은 09:10에서 09:30 KST로 연장. 이후 arm 가능 후보는
  shadow 사유로만 기록하고 주문·Hermes advisor·paper fill은 생성하지 않음
- 한계: 1주 예상 손실이 5,000원 위험 예산을 넘으면 주문 상한과 무관하게
  `below-one-lot`으로 계속 차단됨

### Hermes 대화 화면에 11:50 중간(점심) 및 15:40 마감 패널 브리핑 연동

- 기록 시각: 2026-08-24 10:38 KST
- 배포 시각: 2026-08-24 10:37 KST
- 동작: `/hermes` 웹 페이지 및 `/api/timeline`에서 `daily_analysis_panels` 및
  `daily_analysis_opinions`를 조회하여, 11:50 중간(점심) 분석(`midday`) 및 15:40
  마감 분석(`daily`)의 패널 종합 판정문(`judge:hermes`)과 토큰 사용량을 대화 목록에 연동
- UI: 종류 필터에 `중간 분석`(`midday`) 옵션 추가
- 안전: Read-only PostgreSQL 연결 유지, 무위험 조회

## 2026-08-21

### 이번 주 setup-v2 가격 표본 복원

- 기록 시각: 2026-08-21 17:00 KST
- 복원 시각: 2026-08-21 16:43~16:57 KST
- 코드: 과거 `backfill-intraday-samples`가 최신 페이지에서 시작하던 문제를
  해당 세션 마감 cursor 시작으로 수정. 구 `eligible_rank` NULL run은 승인 후보를
  복원 대상으로 포함하고, 주간 집계가 최근 1,000봉에서 잘리지 않게 보완
- 데이터: 8/18~21 관측 후보 1분봉 61,239개, full-pool 가격 셋업 후보의
  추가 1분봉 12,240개, 8/14 cutoff 일봉 warmup 10,900개 복원. 모든 수집은
  `TRADING_ENABLED=false`, 주문 호출 0
- 진단: current static-eligible 357종 중 매일 341종 평가. 가격 셋업 46
  종목-일/고유 25종을 찾고 46개 모두 정규장 1분봉 390개 확보
- 도구: `automation/setup-v2-sample-diagnostic.py`가 read-only
  `price-only-counterfactual` 결과만 출력. exact replay, strict 승인, PnL 지원 안 함

### D-1 setup-first 장전 풀과 변화 중심 점심 브리핑

- 기록 시각: 2026-08-21 14:49 KST
- 후보: 08:35 KST에 실제 조회한 KRX D-1 시세와 현재 PIT 관리 종목의 교집합을 200봉 가격 셋업부터
  전수 평가한 뒤 전일 거래대금으로 재랭크. 가격 셋업 통과자만 최대 15종 고정
- 안전: 09:10 진입 마감, 갭·수량·heat·cash·Risk, 보유 SELL 경로 유지. 정상
  0종은 성공 cache, 수집/파싱 오류는 실패 후 재시도
- 진단: 비승인 후보의 실제 사유가 `waiting:first-session-bar`로 덮이지 않으며,
  늦은 후보의 시초봉 소급 조회도 하지 않음
- 브리핑: 11:50/15:40 n8n trigger와 종류를 명시적으로 분리. 원시 cycle 반복
  대신 사유의 첫값·마지막값·전환 횟수·오류 분류를 담은 compact snapshot 전달
- 상태: 운영 반영 완료
- 기록 시각: 2026-08-21 15:34 KST
- 1차 배포: 15:12 KST automation, 15:13 KST n8n daily·intraday 게시,
  15:14 KST Hermes panel runner 반영. 모두 `TRADING_ENABLED=false`
- 실운영 보완: 15:15·15:20 KST cycle에서 신규상장 종목의 저장된 짧은
  일봉 이력을 inclusive cursor로 이어받지 못해 fail-closed한 사실을 확인.
  `before=oldest`의 Toss 실측 응답이 경계봉 1개와 `nextBefore=null`인 계약을
  반영하고, 부족 수량에 경계 1칸을 더 요청하도록 수정
- 최종 배포·검증: 15:26 KST automation 재배포. 15:29 KST paper-only smoke에서
  D-1 known pool 364종 전수 평가, 가격·PIT·이벤트 승인 8종/선정 8종,
  `exitCode=0`, 종목 실패 0, 체결 0. automation healthy/restart 0,
  n8n 두 workflow active=draft, Hermes runner 해시 일치

## 2026-08-20

### Hermes 후보 탐색 풀을 적격 Top30으로 확대

- 기록 시각: 2026-08-20 14:27 KST
- 동작: Rule은 기존 Top15를 유지. Hermes에는 별도 `hermesSnapshot`으로 정적
  적격 Top30을 전달해 거래대금 16~30위 후보도 setup-v2.3 신호 평가
- 안전: 가격·PIT 6세션·이벤트·갭·Risk·개장 후 10분 하드 게이트는 동일하며
  Hermes가 우회하지 못한다. Hermes 기존 보유 종목은 Top30 밖이어도 SELL 관찰
- 감사: Rule은 `rule-top15`, Hermes는 `hermes-expanded-top30`으로 기록하고
  후보 분모가 달라 직접 A/B 비교가 아님을 명시
- 1차 배포 시각: 2026-08-20 14:30 KST
- 검증: 2026-08-20 14:35 KST 첫 Top30 실행은 직렬화된 `maStates` 길이 불일치로
  fail-closed. Rule/Hermes 체결 0, n8n 실패 Telegram 정상 발송
- 기록 시각: 2026-08-20 14:36 KST
- 보완: Top30 및 보유종목 확장 snapshot의 `maStates`를 symbols 길이에 맞춤
- 2차 배포 시각: 2026-08-20 14:37 KST
- 운영 검증: 2026-08-20 14:38 KST Rule snapshot 15종, Hermes 준비·실제 평가
  30종, sample 실패 0, 양쪽 체결 0. Hermes `evaluationPool`은
  `hermes-expanded-top30`·직접 비교 불가로 저장. automation healthy,
  restart 0, `TRADING_ENABLED=false`
- n8n: MCP 공개·active, draft=active, intraday Rule→Hermes graph 유지

### 1분봉 수집 풀을 적격 후보 30종으로 확대

- 기록 시각: 2026-08-20 14:06 KST
- 문제: 매매 universe 15종만 1분봉을 저장해 순위 교체 종목의 장중 표본이 비었다
- 동작: 매매 대상은 15종 그대로 유지. 같은 ranking의 정적 적격 Top30을 별도
  수집 풀로 보존하고, cycle 밖 후보에 최근 1분봉 30개를 매 cycle upsert
- 관측: 수집 대상·수신/저장 봉 수·실패 종목을 `intradaySample`에 기록. 추가 수집
  실패는 현재 매매 cycle을 중단하지 않는다
- 한계: 배포 이후 데이터만 축적하며 과거 공백은 자동 복원하지 않는다
- 복원: `backfill-intraday-samples --as-of YYYY-MM-DD`는 해당일 관측된 후보
  합집합을 cursor로 최대 5페이지 조회해 장 시작 도달 여부와 복원 봉 수를 기록
- 기록 시각: 2026-08-20 14:17 KST
- 배포 시각: 2026-08-20 14:18 KST automation 배포
- 복원 시각: 2026-08-20 14:19 KST
- 운영: 오전·오후 관측 후보 합집합 51종 모두 장 시작까지 cursor 수집 완료.
  실패 0, 신규 복원 1분봉 11,964개, 종목당 2페이지. 실행 후 automation
  healthy, restart 0, `TRADING_ENABLED=false`

### setup-v2.3 수급 반전을 필수 차단에서 가점으로 전환

- 기록 시각: 2026-08-20 13:40 KST
- 동작: PIT 수급 연속 6세션은 계속 필수. 외국인 반전이 없다는 이유만으로 가격
  셋업 후보를 차단하지 않고, 외국인·기관 확인은 `flow_stars` 가점으로 유지
- 안전: 가격 셋업, RSI 추격, 낙하 칼날, 임박 이벤트, 갭, Risk는 그대로 차단
- 안전 보완: 신규 진입 arm은 개장 후 10분까지만 허용한다. 늦은 배포·재시작은
  `late-entry-window`로 막고, 기존 포지션 청산은 장중 계속 평가한다
- 배포 시각: 2026-08-20 13:47 KST automation 배포, 13:50 KST n8n MCP 검증·게시
- 운영: `main` `571d34c`까지 푸시. automation healthy, `TRADING_ENABLED=false`,
  n8n draft/active version 동일, 16개 노드 검증 통과, MCP 공개 유지

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
