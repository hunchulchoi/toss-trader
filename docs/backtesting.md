# MA 백테스트

`backtest-ma`는 저장된 캔들을 시간순으로 재생해 이동평균 교차 전략을 평가한다.
Toss Trader 운영 장부와 주문 API에는 쓰지 않는 read-only 명령이다.

```bash
PYTHONPATH=src python3 -m toss_trader backtest-ma 005930 \
  --interval 1d \
  --count 1000 \
  --short-window 20 \
  --long-window 60 \
  --quantity 1 \
  --slippage-bps 5 \
  --initial-cash 1000000
```

PostgreSQL을 사용할 때는 `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`를 주입한다. 운영 Toss DB 포트는 `5431`이다.

## 체결 모델

- 캔들 종가에서 신호를 계산하고 다음 캔들 시가로 체결한다.
- 마지막 캔들에서 발생한 신호는 다음 캔들이 없으므로 체결하지 않는다.
- `--slippage-bps`는 매수 가격을 올리고 매도 가격을 내리는 불리한 방향으로 적용한다.
- 골든크로스에서 포지션이 없으면 지정 수량을 매수한다.
- 데드크로스에서 보유 수량 전부를 매도한다.
- 현금이 부족한 매수 신호는 체결하지 않는다.
- 마지막에 열린 포지션은 강제 청산하지 않고 마지막 종가로 평가한다.
- 수수료·세금은 paper trading과 같은 `toss_trade_costs`를 사용한다.
- 국내 보통주는 매매 수수료 0.015%, 매도 거래세 0.20%를 원 미만 절사한다.
- 열린 포지션의 미실현손익에는 미래 매도 수수료·세금을 미리 차감하지 않는다.

호가 잔량과 부분 체결은 반영하지 않는다. 슬리피지는 고정 비율이며 다음 시가에
적용한다. 결과는 전략 비교용이며 실제 수익을 보장하지 않는다.

## 결과

주요 JSON 필드는 다음과 같다.

| 필드 | 의미 |
|---|---|
| `final_equity` | 마지막 현금 + 보유수량 × 마지막 종가 |
| `total_return_rate` | `(final_equity - initial_cash) / initial_cash` |
| `buy_hold_return_rate` | 첫 종가부터 마지막 종가까지의 비용 미반영 보유수익률 |
| `excess_return_rate` | 전략수익률 - Buy & Hold 수익률 |
| `max_drawdown_rate` | 평가금액 고점 대비 최대 하락률 |
| `realized_pnl` | 완료된 매매의 비용 차감 손익 |
| `unrealized_pnl` | 열린 포지션의 현재 평가손익 |
| `total_costs` | 실제로 체결된 수수료 + 세금 |
| `win_rate` | 이익으로 끝난 완료 매매 / 전체 완료 매매 |
| `slippage_rate` | 체결가에 적용한 불리한 방향의 슬리피지 비율 |
| `trades` | 체결 시각·방향·가격·수량·비용·실현손익 |

## 다종목 포트폴리오

`backtest-portfolio-ma`는 여러 종목의 독립적인 MA 신호를 하나의 현금 잔액으로
재생한다. paper 장부와 주문 API에는 쓰지 않는다.

```bash
PYTHONPATH=src python3 -m toss_trader backtest-portfolio-ma \
  005930 000660 069500 \
  --interval 1d \
  --count 1000 \
  --short-window 20 \
  --long-window 60 \
  --quantity 1 \
  --slippage-bps 5 \
  --initial-cash 10000000 \
  --format json
```

포트폴리오 체결·평가 규칙:

- 각 종목은 자기 캔들 종가로 신호를 만들고 자기 다음 캔들 시가에 체결한다.
- 종목별 캔들 시각이 달라도 전체 타임스탬프 합집합을 시간순으로 재생한다.
- 같은 시각의 주문은 종목코드 오름차순으로 처리해 결과를 결정적으로 만든다.
- 모든 종목이 현금을 공유한다. 앞 주문 체결 뒤 현금이 부족하면 뒤 매수는 건너뛴다.
- 보유 종목은 각 종목의 마지막 종가로 평가한다. 마지막 신호는 강제 체결·청산하지
  않는다.
- 전체 평가액 갱신은 같은 타임스탬프의 모든 체결·종가 반영 뒤 한 번 수행한다.
- 수수료·세금·슬리피지는 단일 종목과 같은 규칙이다.
- 종목별 캔들은 같은 interval과 통화를 사용해야 하며 종목마다 최소
  `long_window + 1`개가 필요하다.

`buy_hold_return_rate`는 각 종목의 첫 종가→마지막 종가 무비용 수익률을 동일
가중한 값이다. 리밸런싱·수수료·세금은 포함하지 않으므로 전략 비교용 벤치마크다.

JSON은 전체 집계, `positions` 종목 요약, `trades` 전체 체결 내역을 담는다. CSV는
종목당 한 행이며 `portfolio_*` 전체 집계를 반복하고 `symbol_*` 종목 요약을 담는다.
`insufficient_cash_buys`는 공유 현금 부족으로 건너뛴 매수 신호 수다. 종목별
`average_cost`는 매수 수수료를 포함한 보유 원가를 수량으로 나눈 값이다.

```bash
PYTHONPATH=src python3 -m toss_trader backtest-portfolio-ma \
  005930 000660 069500 --format csv > portfolio-backtest.csv
```

## 파라미터 검증

여러 MA 조합을 학습 구간과 이후 검증 구간으로 나눠 비교한다.

```bash
PYTHONPATH=src python3 -m toss_trader walk-forward-ma 005930 \
  --interval 1d --count 1000 \
  --short-windows 5 10 20 \
  --long-windows 20 40 60 \
  --train-ratio 0.7 \
  --quantity 1 --slippage-bps 5
```

학습 순위는 초과수익률 내림차순, MDD 오름차순, 총수익률 내림차순으로 정한다.
`selected_short_window`와 `selected_long_window`는 검증 결과를 보지 않고 학습
1위로 선택한다. 각 조합에는 별도의 `validation_rank`도 제공한다.

`overfit_warning`은 다음 중 하나면 `true`다.

- 학습 초과수익이 양수였지만 검증 초과수익이 0 이하
- 학습 또는 검증 구간의 완료 매매가 0건

두 구간은 독립적으로 시작하며 포지션을 넘기지 않는다. 검증 구간 앞부분의
`long_window + 1`개 캔들은 이동평균 준비에 사용되며 다음 캔들부터 체결할 수 있다.

JSON/CSV 파일 저장:

```bash
PYTHONPATH=src python3 -m toss_trader walk-forward-ma 005930 \
  --short-windows 5 10 20 --long-windows 20 40 60 \
  --format json > walk-forward.json

PYTHONPATH=src python3 -m toss_trader walk-forward-ma 005930 \
  --short-windows 5 10 20 --long-windows 20 40 60 \
  --format csv > walk-forward.csv
```
