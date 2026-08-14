# Paper 손익 엔진

Toss Trader의 손익 엔진은 `paper_fills`를 체결 순서대로 재생해 포지션 원가와
실현손익을 계산하고, 최신 시세와 현금잔고를 합쳐 총자산과 일일 수익률을 만든다.
실제 주문이나 세무 신고용 손익 계산은 지원하지 않는다.

## 출력값

`PortfolioPerformance.daily()`는 다음 값을 반환한다.

| 필드 | 의미 |
|---|---|
| `equity` | 비용 반영 현금잔고 + 보유 종목 평가금액 |
| `realized_pnl` | 매도 완료 수량의 누적 순실현손익 |
| `unrealized_pnl` | 현재 평가금액 - 잔여 포지션 원가 |
| `total_costs` | 전체 체결의 누적 수수료 + 세금 |
| `daily_return_rate` | UTC 일자 시작 총자산 대비 현재 총자산 수익률 |
| `currency_returns` | 통화별 직전 기준가 대비 보유 평가금액 수익률 |

cycle JSON은 위 값을 각각 `equity`, `realizedPnl`, `unrealizedPnl`,
`totalCosts`, `dailyReturnRate`, `currencyReturns`로 노출한다.

## 이동평균 원가

종목마다 다음 누적 상태를 유지한다.

- `q`: 보유 수량
- `B`: 잔여 원가
- `R`: 누적 실현손익
- `C`: 누적 수수료
- `T`: 누적 세금

매수 체결 수량 `qb`, 체결금액 `Nb`, 수수료 `Cb`, 세금 `Tb`:

```text
q' = q + qb
B' = B + Nb + Cb + Tb
R' = R
```

매도 체결 수량 `qs`, 체결금액 `Ns`, 수수료 `Cs`, 세금 `Ts`:

```text
배분원가 = B × qs / q
순매도대금 = Ns - Cs - Ts
q' = q - qs
B' = B - 배분원가
R' = R + 순매도대금 - 배분원가
```

전량매도 후 `B`는 나눗셈 잔여값 없이 `0`으로 고정한다. 보유량보다 큰 매도
체결이 장부에 있으면 손익 계산을 중단하고 오류를 낸다.

체결 순서는 SQLite에서 `(executed_at, rowid)`, PostgreSQL에서
`(executed_at, fill_sequence)`를 사용한다.

### 계산 예시

국내주식에 다음 체결이 있다고 가정한다.

1. 10주 × 10,000원 매수, 수수료 15원
2. 10주 × 12,000원 매수, 수수료 18원
3. 5주 × 15,000원 매도, 수수료 11원, 세금 150원

두 번의 매수 후 원가는 `220,033원`, 평균원가는 `11,001.65원`이다. 5주
매도에 배분되는 원가는 `55,008.25원`, 순매도대금은 `74,839원`이다.

```text
실현손익 = 74,839 - 55,008.25 = 19,830.75원
잔여원가 = 220,033 - 55,008.25 = 165,024.75원
누적비용 = 15 + 18 + 11 + 150 = 194원
```

## 현금과 평가손익

체결별 현금 변화:

```text
BUY  = -(체결금액 + 수수료 + 세금)
SELL = +(체결금액 - 수수료 - 세금)
```

포트폴리오 값:

```text
평가금액 = Σ(보유수량 × 현재가격)
총자산 = 현금잔고 + 평가금액
미실현손익 = 평가금액 - 잔여원가
```

미실현손익은 아직 발생하지 않은 향후 매도 수수료·세금을 차감하지 않는다.
매도가 체결되는 시점에 해당 비용이 실현손익과 현금잔고에 반영된다.

## 시세 선택

열린 포지션마다 다음 순서로 평가가격을 고른다.

1. 일봉 2개가 있으면 이전 일봉과 현재 일봉의 종가 사용
2. 일봉이 0~1개면 최신 1분봉을 현재가격으로 사용
3. 이때 이전 기준값은 장부의 비용 포함 잔여원가 사용
4. 최신 평가가격이 없거나 통화가 바뀌면 fail-closed 오류

이 규칙 때문에 장중 신규 포지션도 일봉 생성 전 최신 1분봉으로 평가할 수 있다.

## 일일 baseline과 RiskManager

단일 통화 포트폴리오는 UTC 일자별 최초 계산에서 다음 시작 총자산을 만든다.

```text
시작 총자산 = 현재 현금잔고 + Σ(이전 기준가격 × 현재 보유수량)
일일 수익률 = 현재 총자산 / 시작 총자산 - 1
```

시작 총자산은 `paper_portfolio_daily_baselines`에 `(portfolio_id,
trading_day)` 단위로 한 번만 저장한다. 이후 같은 UTC 일자의 모든 cycle이 같은
값을 사용한다. 계산 결과는 매 cycle `paper_portfolio_snapshots`에 upsert한다.

cycle은 주문 전 손익으로 RiskManager를 평가한다. 체결이 생기면 다시 계산해
해당 cycle의 수수료·세금과 포지션 변화를 최종 snapshot 및 cycle JSON에 반영한다.
`daily_return_rate <= -0.03`이면 `daily-loss-limit`으로 신규 `BUY`를 차단한다.
보유 포지션을 줄이는 `SELL`은 손실 한도 이후에도 허용한다.

여러 통화 포지션이 동시에 열려 있으면 환율 변환 기준이 없으므로 총자산 일일
수익률을 만들지 않는다. 이 경우 `currency_returns` 중 최저값을
`daily_return_rate`로 사용한다.

## 저장 장부

| 테이블 | 역할 |
|---|---|
| `paper_fills` | 체결금액, 수수료, 세금과 체결 순서의 원천 장부 |
| `paper_portfolio_daily_baselines` | 포트폴리오별 UTC 일자 시작 총자산 |
| `paper_portfolio_snapshots` | cycle 시점 총자산, 실현·미실현손익, 누적비용 |
| `paper_cycle_runs` | 최종 `daily_return_rate`와 cycle 상태 |
| `paper_risk_decisions` | RiskManager가 실제로 사용한 일일 수익률 |

SQLite와 PostgreSQL 구현은 같은 계산 규칙과 필드를 제공한다. snapshot은 같은
`(portfolio_id, captured_at)`이면 최신 계산값으로 갱신한다.

Telegram MCP `toss_paper_holdings`와 `toss_paper_pnl`도 이 이동평균 재생을
쓴다. 스냅샷 테이블을 읽지 않고 조회 시점에 `paper_fills`를 다시 계산한다.

## 현재 제한

- 이동평균법만 지원한다. FIFO·세무 신고 원가는 지원하지 않는다.
- 배당, 액면분할, 병합, 합병, 유상·무상증자 같은 권리변동은 반영하지 않는다.
- 환율과 환전수수료가 없어 서로 다른 통화의 총자산을 합산하지 않는다.
- 일자 경계는 거래소 현지일이 아닌 UTC다.
- 향후 매도 비용은 미실현손익에서 미리 차감하지 않는다.
- NXT 체결, 국내 ETF 거래세 면제, 계정별 수수료 프로모션은 구분하지 않는다.

핵심 회귀 테스트는 `tests/test_paper.py`, `tests/test_portfolio.py`,
`tests/test_cycle.py`에 있다.
