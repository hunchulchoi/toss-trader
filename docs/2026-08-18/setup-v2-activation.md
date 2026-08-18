# Strict setup-v2 paper 활성화

2026-08-18부터 rule·Hermes paper의 모든 BUY 후보에 strict setup-v2 사전
게이트를 적용한다. 실거래 주문은 계속 비활성이다.

## 활성 경로

```text
MA BUY 후보
  → 일봉 200개
  → 눌림 또는 확인형 RSI 반전
  → PIT 외인 5일 전환
  → 이벤트·3% 갭·추격·급락 금지
  → 통과 시에만 Hermes/RiskManager/paper
```

SELL은 setup-v2를 우회한다. 보유 종목의 모든 추가 BUY는 continuation 여부와
무관하게 `already-held`로 차단한다. Rule이 만든 shared snapshot에 v2 차단
결과가 포함되므로 Hermes 포트폴리오도 같은 시장 입력과 같은 entry gate 결과를
사용한다.

## 현재 동작

유효 PIT 수급과 이벤트 일정 provider가 아직 없다. 이를 `false`로 간주하지 않고
`setup-v2:missing:flow-history,missing:event-calendar`로 기록한다. 따라서 현재
신규 BUY 0건이 의도한 fail-closed 결과다. cycle funnel에는
`setupV2Blocked`, 종목 결과에는 `skip_reason`과 `idle_reason=setup-v2-block`이
남는다. v2에서 차단된 후보는 Hermes token과 RiskManager 판단 행을 만들지 않는다.

## 남은 연결

- 유효 `available_at`을 가진 6세션 외인·기관 수급 provider
- 사전 공지 일정과 사후 수시공시를 분리한 이벤트 provider
- 구조적 stop, ATR, open/cluster heat를 보존하는 실행 사이징
- MA 후보 게이트가 아닌 독립 setup-v2 entry generator 전환

위 입력 전에는 기존 고정 수량으로 우회하지 않는다. 운영 n8n·risk cap·SELL
청산 규칙은 이번 변경에서 수정하지 않았다.
