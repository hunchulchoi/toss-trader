# 2026-08-14 Cursor paper 성과·프로세스 리뷰

매매 권고 아님. paper 장부 읽기 전용 리뷰다. `TRADING_ENABLED=false`.
코드·DB·Docker·n8n·Infisical은 이 문서를 쓰면서 조회하거나 변경하지 않았다.

## 분석 범위와 시각

- 역할: Cursor 리뷰어. 브랜치 `agent/cursor`.
- 대상일: 2026-08-14 KST 정규장 paper cycle (장전 08:30, 장중 09:00~15:20 1m, 마감 15:40 1d).
- 포트폴리오: `rule`, `hermes`. `legacy`는 비교 성과에서 제외.
- 질문: Rule/Hermes별 신호·체결·보유·실현/미실현·거부·실행 오류를 근거로 전략·운영 개선안. 특히 당일 `max_open_positions` 5→10과 Telegram JSON 수정이 성과/리스크 판단을 왜곡하는지.
- 리뷰 대화 시각: 2026-08-14 15:46~15:51 KST 전후. 마지막 관측 cycle은 마감 1d 15:40 KST.
- 이 문서를 저장하는 시각: 2026-08-14 16:05 KST. 저장 시점에 장부를 다시 읽지 않았다.

## 사용한 근거와 한계

### 근거 (당시 수집)

당시 읽기 전용으로 본 원천:

- 공용 Hermes용 `paper-mcp` (`toss_paper_status` / `holdings` / `pnl`). 포트 미공개라 컨테이너 내부 `127.0.0.1:8090`만 호출.
- 같은 세션의 `paper_fills`, `paper_risk_decisions`, `paper_cycle_runs`, `automation_run_logs` 집계 SELECT.
- `toss-trader automation-runs` CLI, 컨테이너 상태, Alertmanager Telegram 실패 counter.
- 코드: `RiskLimits`, `_pick_idle_reason` / `IDLE_PRIORITY`, `paper_cycle_notice`의 `max-open-positions` 제외, CLI `risk-decisions`의 `legacy` 기본값.
- changelog·시나리오·이전 대화의 agy 주장 (10:30 KST: 5종 부족, 슬롯 10, daily 5 유지).

당시 관측 스냅샷 (`TRADING_ENABLED=false`, paper-mcp healthz tools=3, automation health 200):

| 항목 | Rule | Hermes |
|---|---:|---:|
| 현금 | 219,609 (22%) | 396,173 (39%) |
| 총자산 | 988,486 | 1,005,840 |
| 시작현금 대비 | -11,514 (-1.15%) | +5,840 (+0.58%) |
| 실현 | -4,839 | -3,149 |
| 미실현 | -6,675 | +8,989 |
| 보유 | 4종 / 6주 | 7종 / 7주 |
| 마감 `dailyReturnRate` | +1.85% | +1.54% |
| 장중 1m cycle | 81 | 79 |
| 장중 신호 합 | 150 | 135 |
| 장중 체결 | 7 (BUY 5 + SELL 2) | 7 (BUY 5 + SELL 2) |
| 종목 처리 실패 합 | 0 | 19 (13:55 1회) |

보유 스냅샷:

- Rule: 005930 1주, 042700 2주, 134580 1주, 469610 2주.
- Hermes: 005180, 005930, 042700, 066430, 134580, 332570, 388050 각 1주.

거부 스냅샷 (KST 일자 `paper_risk_decisions`):

- 10:55 전: `max-open-positions` hermes 43 / rule 42. `max-daily-buys` 0.
- 10:55 후: 슬롯 0. `max-daily-buys` hermes 83 / rule 106.
- 마감창 혼합 (`max-daily-buys` + `market-close-window`) 각 6.
- Hermes advisor 거부 1건: 09:00 042700 trend entry.

운영 스냅샷:

- `특이사항 Telegram` 실패 74회 (09:00~15:10).
- 15:09 automation 재빌드 후 15:10은 다시 실패, 15:15부터 success.
- 15:15·15:20 `telegram-report` 성공. 내용은 `max-daily-buys` (15:20는 마감창 혼합).
- Alertmanager Telegram `notifications_failed_total` 0.
- hermes_trade 성공 8건, 분석 sidecar 마감 1건. 장중 hermes-cycle 대부분 token 0 (한도 preflight skip이 정상).
- CLI `risk-decisions` 기본 포트폴리오는 `legacy`. 오늘 rule/hermes 판단이 그 명령만으로는 안 보였다.

### 한계 — 재검증 필요

위 수치는 **Infisical 단일 원천·machine token 비출력 규칙이 `AGENTS.md`에 강화되기 전**에 수집됐다. 당시 paper-mcp·automation 컨테이너 환경의 DB 접속 경로를 사용했다. 강화된 규칙은 환경별 DB 접속정보를 Infisical에서만 주입하고, 컨테이너 env·Compose·프로세스 상태에서 유추하지 말라고 한다.

따라서 이 문서의 금액·건수·시각은 **동결된 1차 관측**이다. 운영 판단·changelog 확정·전략 변경의 근거로 쓰려면 Infisical 주입 읽기 전용 경로로 같은 질의를 다시 돌려 재검증해야 한다. 이 문서를 저장하는 턴에서는 DB/Infisical/secret 조회를 하지 않았다.

기타 한계:

- MCP holdings/pnl은 조회 시점 fills 재생이다. Grafana `paper_portfolio_snapshots`와 어긋날 수 있다.
- `dailyReturnRate`는 UTC 일자 시작 총자산 대비. 시작현금 대비 누적과 분모가 다르다.
- 13일 마감 수익률(+2.84% / +3.73%)은 비용 반영 엔진 도입 전 MTM이다. 오늘 누적과 직접 비교하지 않는다.
- LLM token 사용량은 자격증명이 아니다. advisor 호출 여부 판별용으로만 적는다.

## 오늘 이벤트 타임라인

시각은 KST. 배포 시각은 changelog·git·컨테이너 Created 기준의 당시 관측이다.

| 시각 | 사건 |
|---|---|
| 전일 마감 | Rule 오버나잇 3종(005930, 134580, 469610). Hermes 4종(005930, 134580, 332570, 388050). |
| 08:30 | 장전 시장분석. 주문 없음. |
| 09:00 | Rule: 034020·042700 trend entry. Hermes: 034020 entry, 042700 advisor 거부. 이후 둘 다 5종. |
| 10:22 | continuation + `idleReason` 이미지. paper-mcp 재빌드 관측. |
| 10:27 | 수동 배포 테스트. hermes-cycle 실패: rule cycle JSON 없음 (`cont-deploy`). |
| 10:30 | 운영 스냅샷: Rule 현금 약 382,353 / Hermes 약 623,136, 각 5종. 거부 각 29건이 `max-open-positions`. agy: 5종 부족, 슬롯 10, daily 5 유지, 오늘 +5종 가능. |
| 10:40 | Rule 469610 골든크로스. 기존 1주 위에 추가. 슬롯 5여도 가능 (신규 종목이 아님). |
| 10:44 | 커밋 `5bc591c`. `max_open_positions` 5→10. `daily-loss-limit`은 BUY만. |
| 10:55 | 첫 6번째 종목. 양쪽 459550 continuation. 이후 슬롯 거부 0, daily-buys가 병목. |
| 11:05 | 042700 골든크로스. Rule은 2주. Hermes는 09:00 거절분을 226,500에 매수 (Rule 09:00 진입 239,500). |
| 11:10 | Hermes 005180·066430 trend entry. 양쪽 daily buy 5 도달. |
| 11:35 | 034020 데드크로스 매도 (84,200→81,800). |
| 11:50 | 459550 데드크로스 매도 (2,385→2,325). 10:55 continuation의 당일 왕복. |
| 13:55 | Hermes 1m 실패. DNS `Name or service not known`으로 19종 `portfolio-risk`. exit 3. Rule 실패 0. |
| 15:09 | automation 재빌드. n8n `toss-trader-intraday-paper` import·재활성. JSON spread 제거, 슬롯 단독 거부 알림 제외. |
| 15:10 | 사이클 자체 success, `특이사항 Telegram`은 또 실패. JSON 수정 후 첫 슬롯은 놓침. |
| 15:15 | 정규 실행 success. Rule 007340 `max-daily-buys` 알림은 살아 있음. |
| 15:20 | Rule 237880 `max-daily-buys` + `market-close-window`. telegram-report success. |
| 15:40 | 마감 1d. 둘 다 19종, 신호 5, 체결 0, `risk-block` 5. `idleReason=no-crossover`. |

## 성과 왜곡 요인

세 변화가 같은 거래일에 겹쳤다. 하루 PnL과 Rule vs Hermes를 전략 효과로 읽으면 안 된다.

1. **슬롯 5→10을 장중(10:44~10:55)에 넣음.** 오전은 5슬롯 실험, 10:55 이후는 10슬롯+daily 5. agy의 “오늘 +5종”과 불일치. Hermes는 슬롯 개방 후 BUY 4건 중 459550이 55분 왕복. Rule 신규 종목은 459550 하나이고 나머지는 기존 종목 가산.
2. **병목이 슬롯에서 `max_daily_buy_count=5`로 이동.** 10:55 이후 슬롯 거부 0. 남은 현금(Rule 22% / Hermes 39%)은 “전략이 안 산다”가 아니라 하루 매수 5회 + 1주 기본 + 골든크로스 가산이다. 슬롯 10은 오후에 일을 하지 않았다.
3. **Telegram JSON 수정은 체결이 아니라 관측을 바꿈.** 74회 critical은 사이클을 막지 않았다. Alertmanager Telegram 실패 counter는 0이라 채널이 건강해 보인다. 슬롯 거부는 알림에서 빠졌고, 실제 오후 거부는 daily-buys다. JSON이 오전에 살아 있었으면 슬롯 도배가 daily-buys 도배로 바뀌었을 뿐이다. 15:15 이후 그 도배가 시작됐다.

분모 혼선:

- MCP 누적 = 시작현금 1,000,000 대비. Rule은 적자.
- cycle `dailyReturnRate` = UTC 일자 시작 총자산 대비. 둘 다 플러스.
- 어제 마감 %는 옛 MTM. 세 숫자를 한 문장에 넣으면 “오늘 벌었다”로 왜곡된다.

비교 실험 오염:

- 09:00 042700은 advisor 차이 (Hermes 거부, Rule 매수).
- 11:05 같은 종목 재진입은 슬롯 10 + 다른 신호(골든크로스) + 다른 가격.
- 원인 하나를 고르지 못하면 A/B가 아니다.

`idleReason` / `newBuysAllowed`:

- 마감 cycle은 신호 5개가 전부 한도 거부인데 다수결이 `no-crossover` (14 vs 5). `IDLE_PRIORITY`가 `no-crossover`를 `risk-block`보다 앞에 둔다.
- `newBuysAllowed=true`는 universe 갱신 플래그. 마감창·daily 캡과 무관하다. “지금은 사도 된다”로 읽히면 오진이다.

## 전략 문제

- 1분 continuation + 1분 데드크로스. 459550 10:55 매수 → 11:50 매도. 034020 09:00~11:35 왕복. 당일 실현 손실의 큰 덩어리다.
- “종목당 1주”가 장부와 불일치. 슬롯 검사는 신규 종목만 본다 (`position_quantity <= 0`). 골든크로스는 기존 포지션에 더한다. Rule 한미반도체 원가 466,033 ≈ 시드 47%.
- 슬롯만 10으로 올리고 daily 5를 유지하면, 이미 5종을 든 날에는 오전에만 추가 매수가 열리고 곧 daily 캡에 걸린다. 슬롯 확대의 실험 기간이 수 시간이 된다.
- 교체매매 없음. 더 좋은 후보가 와도 기존 종목과 바꾸지 않는다. 슬롯을 열면 신규만 쌓이고, 연 직후 산 continuation이 당일 죽는다.
- 손절·익절 코드 없음. 청산은 데드크로스에만 의존한다. 진입을 넓힌 날의 왕복 손실을 사이징/청산 부재와 분리하지 못한다.

## 운영 문제

- README는 여전히 “하루 최대 매수와 동시 보유는 각각 5종목”. 코드·시나리오는 슬롯 10, daily 5.
- changelog 슬롯 항목에 운영 시각이 없다. JSON 수정만 15:09 / 15:15가 적혀 있다.
- `toss-trader risk-decisions` 기본 `legacy`. 오늘 비교 장부 200건 이상을 그 명령만으로 못 본다.
- JSON 실패 74회를 Alertmanager 실패 counter 0과 같게 보면 안 된다. 진실은 `automation_run_logs` stage `특이사항 Telegram`.
- 15:09 재빌드와 15:10 사이클 사이에 한 틱이 남았다. import·재활성 직후 첫 정규 실행을 성공으로 단정하지 말 것.
- Hermes 13:55 DNS는 단발인지 미확인. 같은 시각 Rule은 실패 0. 비교 실험의 한쪽만 19종 오류로 비었다.
- 장중 한도·알림·continuation을 같은 날 같이 넣으면 다음 날 플러스를 어느 변경 덕분이라고 말할 대조군이 없다.

## 지금 수정할 것 / 관찰할 것

장 종료 후 한도를 더 올리지 않는다. 원인 분리가 먼저다. 아래는 할당이 오면 할 일이지, 이 문서 턴에서 구현하지 않는다.

### 수정 후보

- 리뷰 분모를 세 줄로 고정: 시작현금 대비 누적 / UTC `dailyReturnRate` / 당일 체결만.
- `max-open-positions`와 같은 이유로 `max-daily-buys` 단독 반복도 첫 도달 1줄 또는 요약. 아니면 장중 도배가 다시 시작된다.
- 체결 0일 때 `idleReason`은 막은 이유를 우선 (`risk-block` / `advisor-reject` > `no-crossover`).
- CLI `risk-decisions --portfolio rule|hermes`.
- README의 5종 문구를 슬롯 10·daily 5와 맞춘다.
- 실험 가정이 “1주×N종 분산”이면 골든크로스 가산을 막거나 사이징을 명시한다.

### 관찰 (다음 온전한 1거래일, 장중 한도 변경 없이)

- 슬롯 10 + daily 5가 오전부터 같이 도는지. 오늘처럼 오전이 5·오후가 10이면 그 날 데이터는 버린다.
- 실제로 10종까지 가는지, 아니면 또 daily 5에서 멈추는지.
- continuation 당일 왕복 빈도. 459550 패턴 재발이면 1분 continuation을 전략 개선으로 보지 않는다.
- JSON 수정 이후 `max-daily-buys` 알림이 5분마다인지.
- 13:55 DNS가 단발인지.
- Alertmanager failed_total=0을 건강 신호로 쓰지 않는다.

재검증 전에는 슬롯 10 효과를 확정하지 않는다.

## agy 주장에 대한 반론 질문

원문 요지 (10:30 KST): 5종은 너무 적다. 권장 슬롯 10, `max_daily_buy_count` 5 유지. 오늘 포트폴리오당 +5종. 1주면 10종도 과분산 아님. 숫자만 올리면 된다. 거부 도배는 UX.

1. 슬롯 10 이후 슬롯 거부는 0건, daily-buys는 189건이다. 빈 슬롯이 병목이라는 가설은 오후에 죽었다. 다음은 왜 슬롯이 아니라 daily 5를 안 손대나?
2. “오늘 +5종”의 체결 목록이 뭔가? Hermes는 개방 후 BUY 4건이고 1건은 55분 왕복이다. Rule 신규 종목은 459550 하나다.
3. 10:55 첫 6번째 종목이 continuation 459550이고 11:50 데드크로스로 청산됐다. 슬롯을 연 결과가 추세 추종인가, 1분 잡음인가?
4. Rule은 슬롯 4종인데 한미반도체 2주 원가가 시드 절반에 가깝다. “1주×10종 분산” 가정이 이미 깨졌다. 골든크로스 가산을 알고도 슬롯만 올렸나?
5. 같은 대화에서 8종 완충을 말하고 10:44에 10으로 점프했다. 장중 컷오버의 기준은 뭔가? 하루 데이터를 버리는 비용을 계산했나?
6. 슬롯 거부를 알림에서 빼면, 병목이 daily-buys로 옮긴 뒤 운영자는 한도 포화를 어디서 보나? 텔레그램은 15:15부터 daily-buys를 다시 보낸다.
7. JSON 74회와 슬롯 5→10과 continuation을 같은 날 넣었다. 내일 플러스를 슬롯 덕분이라고 말하려면 대조군이 뭔가?
8. Hermes는 09:00 042700을 거부하고 11:05 같은 종목을 더 싸게 샀다. 이걸 advisor 품질로 쓸 건가, 슬롯 10이 오후 골든크로스를 열어준 결과로 쓸 건가? 둘을 한 문장에 넣지 말고 하나를 골라라.

## 결론

오늘 숫자는 전략 승리/패배가 아니라 **한낮 한도 변경 + 알림 경로 장애 + 1분 continuation 왕복**이 섞인 관측이다. 슬롯 10은 10:55 이후 추가 매수를 잠시 열었고, 곧 daily 5에 막혔다. Telegram JSON 수정은 15:15부터 성공 경로를 복구했지만 관측 편향을 남긴다. Infisical 규칙 강화 전 수치이므로 재검증 없이 한도·전략을 더 바꾸지 않는다.
