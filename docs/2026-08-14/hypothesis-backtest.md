# 전략 가설 백테스트 준비·시나리오 결과

매매 권고가 아니다. `TRADING_ENABLED=false`인 paper 전략 검증 기록이다.

## 검증 범위

실제 저장 캔들 백테스트는 수행하지 않았다. 저장소 안에 로컬 캔들 데이터가
없고, Infisical machine access token이 이전 tool output에 노출돼 revoke/rotate
확인 전 PostgreSQL 접근이 금지돼 있기 때문이다. DB나 실행 중 컨테이너의
credential을 우회 사용하지 않았다.

대신 기존 포트폴리오 엔진이 운영 한도를 전혀 재현하지 못하던 문제를 먼저
수정하고, 결정적 합성 시나리오로 한도 동작과 가설의 방향성을 검증했다. 합성
결과는 수익성 근거가 아니라 엔진 회귀검증과 실험 설계 근거로만 사용한다.

## 엔진 수정

`backtest-portfolio-ma`에 다음 선택 한도를 추가했다.

- `--max-order-notional`
- `--max-position-notional`
- `--max-daily-buys` — production과 같은 UTC 날짜 기준
- `--max-open-positions`

거부 건수는 전체와 종목별 `max_*_rejections`로 JSON/CSV에 기록한다. notional은
production RiskManager와 같이 신호 기준가로 판정하고, 한 신호의 복수 위반을
각 counter에 모두 기록한다. 같은 시각의 신호는 기존처럼 종목코드 오름차순으로
실행·거부한다. 옵션을 생략하면 기존 백테스트 결과와 호환되도록 한도를 적용하지
않는다.

## 가설 1 — daily 5를 10으로 확대

12개 종목이 같은 시각에 MA 상향 교차하는 두 극단 시나리오를 사용했다.
초기현금 1,000,000원, 1주, open 10, order 300,000원, position 1,000,000원,
비용은 기존 Toss paper 비용 규칙이다.

| 시나리오 | daily cap | 총수익률 | MDD | 체결 수 | daily 거부 | open 거부 |
|---|---:|---:|---:|---:|---:|---:|
| 상승 지속 | 5 | 0.015% | 0% | 5 | 7 | 0 |
| 상승 지속 | 10 | 0.030% | 0% | 10 | 2 | 0 |
| 즉시 반전 | 5 | -0.030% | 0.030% | 10 | 7 | 0 |
| 즉시 반전 | 10 | -0.060% | 0.060% | 20 | 2 | 0 |

daily 10은 상승 이익과 반전 손실을 모두 정확히 두 배로 키웠다. 방향 예측력이
검증되지 않은 상태에서는 현금이 있다는 이유만으로 cap을 늘릴 근거가 없다.
`daily=5`를 유지한다. `open=10`은 여러 거래일의 누적 보유를 제한하므로 daily
cap과 중복되지 않는다.

## 가설 2 — position notional을 1,000,000원에서 300,000원으로 축소

정상적인 1주 신규 진입 시나리오에서 order cap 300,000원을 함께 적용했다.
position cap 1,000,000원과 300,000원은 모두 총수익률 7.9922%, 체결 2건,
order/position 거부 0건으로 동일했다.

현재 `max_order_notional=300,000`이 함께 적용되고 continuation은 보유 종목을
건너뛰므로, 정상 단일 진입 경로에서는 position cap 300,000원이 중복이다.
다만 이 엔진도 일반 BUY를 `quantity == 0`일 때만 체결하므로 가산매수 위험을
재현하지 못한다. 따라서 이 결과는 position cap 자체의 실증이 아니다. position
cap만 낮추는 변경은 채택하지 않는다. 집중 문제가 실제로 재현되면 거부된 SELL
뒤 재진입, 중복 cycle, 수량 증가 같은 가산 경로를 먼저 재생 가능하게 만들어야
한다.

## 가설 3 — continuation 직후 dead-cross 청산 완화

현 엔진은 일봉 `RISK_ON`과 1분 continuation을 결합 재생하지 못한다. 실제
1분/일봉 데이터 없이 최소 보유시간이나 ATR 청산을 합성 데이터에 맞춰 넣으면
과적합이므로 전략 코드는 바꾸지 않았다.

다음 단계는 token revoke/rotate 확인 뒤 동일 종목·동일 기간의 1분봉과 일봉을
읽어 다음 세 경로를 walk-forward 비교하는 것이다.

1. 현행 continuation + 즉시 dead-cross
2. continuation 진입에만 최소 보유 candle 적용
3. 현행 체결과 청산 counterfactual 기록만 추가

## 결정

- production/paper 전략·리스크 값 변경 없음
- `max_daily_buy_count=5`, `max_open_positions=10` 유지
- `max_position_notional=1,000,000`, `max_order_notional=300,000` 유지
- 실제 저장 캔들 검증 전 continuation/청산 변경 없음
- 백테스트 엔진과 문서만 수정
