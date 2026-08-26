# 2026-08-26 morning RECLAIM_LOST scope replay

Stored Hermes trade JSON from 09:00–12:00 KST, re-parsed with Hunter-only
`RECLAIM_LOST`. No live LLM call, no fill, not exact engine replay. Sizing
uses the stored Risk quantity and reference price from that cycle.

The four 09:05–09:06 daily experimental rejects were `RECLAIM_LOST` only.
None were Hunter. New scope would have approved all four on the first call.

Noon mark is the last stored 1m close before 12:00 KST. MAE is the session
low from that entry minute through 11:59. Costs are omitted. Not a
profitability claim.

| 종목 | 실제 체결 | 가정 첫 승인 | 12:00 종가 | 실제 평가 | 가정 평가 |
|---|---|---|---|---|---|
| 090360 로보스타 | 09:10 ×1 @ 78639.30 | 09:05 ×1 @ 79739.85 | 79700 | +1061 (+1.35%, MAE -0.81%) | -40 (-0.05%, MAE -2.18%) |
| 079650 서산 | 09:10 ×16 @ 4927.46 | 09:05 ×16 @ 4762.38 | 4605 | -5159 (-6.54%, MAE -8.07%) | -2518 (-3.30%, MAE -4.88%) |
| 067170 오텍 | 09:20 ×18 @ 2596.30 | 09:05 ×18 @ 2621.31 | 2750 | +2767 (+5.92%, MAE -1.01%) | +2316 (+4.91%, MAE -1.96%) |
| 126640 화신정공 | 09:25 ×26 @ 4462.23 | 09:05 ×27 @ 4427.21 | 4395 | -1748 (-1.51%, MAE -3.64%) | -870 (-0.73%, MAE -2.87%) |

합치면 가정 쪽이 덜 잃는다. 주된 차이는 서산이 09:10 추격을 안 한 것.
로보스타·오텍은 지연이 더 싸게 샀다. 같은 규칙이 종목마다 반대 방향.

fixture: `tests/fixtures/2026-08-26_morning_reclaim_lost.json`
