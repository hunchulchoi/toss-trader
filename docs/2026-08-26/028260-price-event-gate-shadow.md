# 삼성물산 028260 가격·이벤트 게이트 shadow 재현

- 기록 시각: 2026-08-26 14:17 KST
- 판정 cutoff: 2026-08-26 09:00:01 KST
- 범위: paper-only, `strategyInput=false`, `shadowOnly=true`
- 원천: `market_candles`, `market_events_pit_v2`, `market_flow_pit_v2`,
  `market_pit_coverage`, 중간 패널 `df1c092a-509c-44d4-b44a-9662946e936f`

## 분리 판정

D-1 완결 200봉의 종가는 372,500원, MA50은 392,230원, MA200은
322,927.5원, MA50 이격은 -5.03%, RSI14는 53.4195다. `close > MA50 >
MA200` 눌림도 아니고 RSI 35 이하 반전도 아니므로 가격만 평가하면
`missing-price-setup` 하나다. 이 가격 위반만 있으면 Hermes 참고형은 후보를 계속
평가할 수 있다.

이벤트 입력은 8월 21일 접수된 `기업설명회(IR)개최(안내공시)`다. 8월 24일
08:00 KST부터 알려졌고, `isEntryBlocking=0`이지만 `isPreannounced=1`,
`scheduledFor=null`이다. 판정시각은 `blockedThrough=8월 26일 08:00 KST`보다
늦지만, 현재 계약은 실현 이벤트가 확인되지 않은 날짜 미상 사전공시를 별도로 계속
차단한다. 따라서 `eventImminent=true`가 되고 최종 위반 순서는
`missing-price-setup → event-imminent`다. `event-imminent`는 Rule과 Hermes 모두의
hard veto라 최종 진입은 막힌다.

## 결론

가격 패턴 미충족과 이벤트 차단은 같은 원인이 아니다. fixture는 가격만 평가한 결과와
공식 PIT context를 합친 결과를 각각 검증한다. 특히 이번 `event-imminent`는
blocked window 때문이 아니라 `scheduledFor=null`인 미해소 preannouncement
fallback 때문이다. IR 공시의 실제 예정일 파싱 또는 종료 조건을 바꿀지는 별도 정책
실험 대상이며, 이 한 건으로 hard gate를 완화하지 않는다.

## 재현 한계

가격 fixture는 알고리즘이 실제 사용하는 200개 종가·최신 시가·직전 고가를 보존한
입력 projection이다. 전체 immutable OHLC snapshot은 아니며, 조회 시점의
current-stored adjusted candle이다. 신호·Risk·주문·fill은 만들지 않는다.
