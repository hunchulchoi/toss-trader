# 2026-08-18~21 setup-v2 가격 표본 복원

매매 권고나 strict setup-v2 수익률 백테스트가 아니다. 현재 known pool과 현재
static metadata를 사용한 `price-only-counterfactual` 진단이다.

## 복원 결과

- 유니버스 관측 후보 1분봉 복원: 8/18 90종, 8/19 90종, 8/20 40종,
  8/21 51종. API 실패 0, 신규 61,239봉
- full-pool 가격 셋업 후보의 남은 분봉 공백 복원: 신규 12,240봉
- 전체 신규 1분봉: 73,479봉
- 8/14 cutoff 일봉 warmup: 426종 조회, 383종 200봉 충족, 신규 10,900봉
- 214680은 8/14 최신봉 부재로 stale 처리. 나머지 미충족은 짧은 상장 이력
- current static-eligible 357종 중 매일 341종을 동일한 200봉 조건으로 평가
- 가격 셋업 46 종목-일, 고유 25종. 모두 pullback
- 가격 셋업 46 종목-일의 09:01~15:30 1분봉 390개를 전부 복원

| 세션 | 가격 셋업 | 완전 1분봉 | 장중 상승 | 시가→종가 평균 |
|---|---:|---:|---:|---:|
| 8/18 | 11 | 11 | 2 | -3.04% |
| 8/19 | 13 | 13 | 8 | +1.40% |
| 8/20 | 10 | 10 | 5 | -0.11% |
| 8/21 | 12 | 12 | 3 | -3.38% |

장중 상승은 18/46이다. 임의의 당일 시가 진입·종가 청산 방향성일 뿐이며
수수료, 슬리피지, 수량, cash, Risk, flow, event, gap gate와 실제 체결을 적용한
PnL이 아니다.

## 재실행

Infisical로 PostgreSQL 설정을 주입한 환경에서 다음 read-only 진단기를 쓴다.

```bash
python automation/setup-v2-sample-diagnostic.py \
  --sessions 2026-08-18 2026-08-19 2026-08-20 2026-08-21
```

출력은 `strictSetupV2Approved=false`, `pnlEvaluated=false`,
`survivorshipBias=current-known-pool-and-current-static-metadata`를 강제한다.

## 해석 제한

- 당시 raw ranking, 당시 static metadata/config, immutable candle revision을 모두
  보존하지 않아 exact historical replay가 아니다.
- 당시 decision cutoff 전에 가용했던 6세션 PIT 수급과 event coverage를 후대
  데이터로 채우지 않는다. strict 승인·거절 수로 해석하지 않는다.
- 5분 cycle 횟수가 아니라 46 종목-일이 가격 신호 표본 단위다.
- 주말에는 price setup 빈도, opening path, gap/사이징 민감도까지만 검사한다.
  승률·알파·실거래 준비 완료 근거로 쓰지 않는다.
