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
