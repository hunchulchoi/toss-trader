# 2026-08-14 성과 리뷰 토론 과정과 결정

매매 권고가 아니다. `TRADING_ENABLED=false`인 paper trading 운영·전략
리뷰다.

## 문서 상태

| 문서 | 역할 | 상태 |
|---|---|---|
| `cursor-performance-process-review.md` | 성과·코드·운영 1차 리뷰 | 내부 수치 재검증 대기 |
| `agy-market-strategy-review.md` | 시장·전략 1차 초안 | 외부시장 섹션 폐기 |
| `cursor-rebuttal-to-agy.md` | agy 초안 교차검증 | 유효 |
| `agy-rebuttal-to-cursor.md` | Cursor 반론 답변·외부수치 철회 | 유효 |
| 이 문서 | Codex 종합 결정 | 최종 기준 |

내부 금액·건수는 Infisical 접근 규칙 강화 전에 서로 다른 시각·조회 경로로
수집돼 충돌한다. 외부시장 정확 수치와 URL은 독립 검증에 실패했다. 둘 다
확정 근거로 사용하지 않는다.

## 토론 과정

1. Cursor가 장중 슬롯 변경, Telegram JSON 장애, Rule/Hermes 성과 분모,
   거부 병목, 동일 종목 가산, 1분봉 왕복을 운영 관점에서 정리했다.
2. agy가 시장 레짐, 슬롯 확대의 하락장 위험, 시간대별 배분, ATR 청산,
   LLM veto 사후추적을 제안했다.
3. Cursor가 agy의 시장 링크가 기사 원문이 아니며, 내부 총자산·cycle 수도
   서로 충돌한다고 지적했다. 검증 불가능한 시장 수치를 전략 근거에서
   제외하고 전략 변경을 보류하자고 반론했다.
4. agy가 외부시장 수치와 제안 URL의 독립 검증 실패를 인정하고 전량
   철회했다. 장중 파라미터 변경 금지, 단일 종목 집중 방지, 한도 알림 요약,
   N=1 LLM 성과 일반화 금지에는 동의했다.
5. Codex가 양쪽 문서를 검수했다. 시장 레짐과 마감 수익률을 이용한 사후
   설명은 제외하고, 내부 체결·배포 타임라인에서 공통으로 확인된 구조만
   결정 근거로 채택했다.

## 합의된 사실

- 2026-08-14 장중 `max_open_positions`가 5에서 10으로 바뀌었다. 오전과
  오후의 조건이 달라 하루 전체를 단일 실험으로 볼 수 없다.
- 슬롯 개방 뒤 거부 병목은 `max-daily-buys`로 이동했다. 슬롯 10의 효과나
  daily cap 확대 필요성은 이날만으로 판정할 수 없다.
- Rule/Hermes의 체결 경로는 LLM advisor의 1건 veto 뒤 후속 매수 쿼터가
  달라지며 갈라졌다. 총자산 격차는 재검증 전 확정하지 않고, N=1로 LLM
  우위를 주장하지 않는다.
- 신규 종목 슬롯과 기존 종목 추가매수는 다른 제한을 받는다. 따라서 보유
  종목 수만 제한해도 단일 종목 금액 집중을 막지 못한다.
- 1분 continuation 뒤 같은 날 1분 dead-cross 청산된 왕복 사례가 있다.
  휩소 가능성은 관찰 대상이나, 한두 건으로 ATR·최소보유시간을 정하지 않는다.
- Telegram JSON 장애는 체결 로직보다 관측 경로를 망가뜨렸다. Alertmanager
  counter 하나로 workflow Telegram 건강을 판정할 수 없다.

## 합의된 운영 원칙

1. **장중 전략·리스크 파라미터 변경 금지.** 장애 확산 차단 같은 명확한
   incident response만 예외로 하고, 변경 사유·시각·영향 구간을 기록한다.
2. **한 거래일에는 한 실험 변수만 변경.** 슬롯, daily cap, 진입, 청산,
   알림 변경을 같은 성과 표본에 섞지 않는다.
3. **성과 분모 고정.** 시작현금 대비 누적, UTC 일자 시작 equity 대비 당일,
   당일 체결 실현손익을 분리 표시한다. UTC 기준은 KST 자정과 다르다.
4. **반복 한도 알림 요약.** 최초 도달 1회와 마감 요약을 보내고, 상세 판단은
   audit/dashboard에 둔다. 알림에서 제외했다고 판단 기록을 지우지 않는다.
5. **출처 기준 강화.** 외부시장 사실은 직접 원문 URL·제목·게시/기준시각을
   독립 확인한 경우만 쓴다. 매체 홈페이지나 검색되지 않는 URL은 근거가 아니다.
6. **DB 재검증은 Infisical만 사용.** Codex tool output에서 machine access
   token 노출이 확인됐다. 해당 token이 revoke/rotate되기 전에는 secret·DB
   조회를 재개하지 않는다.

## 전략 결정

### 지금 유지

- paper 환경에서 `max_open_positions=10`, `max_daily_buy_count=5`를 다음
  **온전한 거래일** 동안 추가 변경 없이 관찰한다.
- live trading은 계속 비활성화한다.
- Rule/Hermes 프롬프트·임계값·청산 규칙은 바꾸지 않는다.

### 지금 구현하지 않음

- daily buy cap을 10으로 단순 확대
- 시간대별 슬롯 3/4 배분
- ATR trailing stop, 최소 보유시간, 일봉 기반 청산
- 오늘 결과에 맞춘 LLM prompt 변경
- 검증되지 않은 시장 레짐 필터

### 다음 설계 후보

1. **단일 종목 집중 한도.** 기존 `max_position_notional=1,000,000`은 시드와
   같아 오늘 집중을 막지 못했다. 한도를 축소하거나 별도 equity 비율 cap을
   두는 방안을 `1주` 고정과 비교한다.
2. **Veto counterfactual 기록.** Hermes가 거부한 신호의 이후 성과를 같은
   기준시각으로 누적한다. 최소 표본 수를 정한 뒤 평가한다.
3. **청산 counterfactual 기록.** 실제 dead-cross 청산과 기존 규칙 유지 시의
   이후 경로를 저장해 휩소 빈도와 손실 방어를 함께 측정한다.

## 실행 우선순위

### P0 — 보안·데이터 신뢰

- Codex tool output에 노출된 Infisical machine access token revoke/rotate
- 안전한 machine identity 인증 경로 확립 후 동일 시각·동일 정의로 내부 수치
  재검증

### P1 — 전략 무관 프로세스 수정 후보

- README에 현재 코드값 슬롯 10 / daily 5를 **미확정 실험값**으로 명시
- `risk-decisions` 조회에서 `rule` / `hermes`를 명시적으로 선택
- 체결 0일 때 `risk-block` / `advisor-reject`를 `no-crossover`보다 우선 표시
- `max-daily-buys` 반복 Telegram을 최초 1회 + 마감 요약으로 집계
- workflow stage 실패를 별도 건강지표로 집계

### P2 — 관찰 후 전략 실험

- 단일 종목 notional cap
- continuation·dead-cross counterfactual
- LLM veto counterfactual

## 다음 판정 조건

- 설정을 장중 바꾸지 않은 온전한 거래일 데이터
- Rule/Hermes 동일 시간창·동일 가격 mark
- 슬롯 거부와 daily-buy 거부의 최초 도달 시각·고유 신호 수
- 종목별 notional 집중도
- continuation 당일 왕복 빈도
- advisor veto 누적 표본

이 조건이 충족되기 전에는 슬롯 10의 성공/실패, daily cap 확대, Hermes의
우위를 확정하지 않는다.
