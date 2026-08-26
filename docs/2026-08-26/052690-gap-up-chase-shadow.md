# 한전기술 052690 gap-up-chase shadow 재생

- 기록 시각: 2026-08-26 10:55 KST
- 시장자료 cutoff: 2026-08-26 10:51 KST
- 범위: paper-only, `strategyInput=false`, `shadowOnly=true`
- 원천: `market_candles`, `paper_cycle_runs`

## 판정 재현

한전기술은 Rule 승인종목이 아니다. D-1 판정은 `missing-price-setup`이었지만,
Hermes v2.3은 이를 참고 위반으로만 두고 실험 후보로 평가했다. signal close
101,700원 대비 첫 완결 1분봉 시가는 110,200원으로 갭은 +8.3579%다. 3% hard
gate를 넘어 09:05부터 저장 cycle의 `setup-v2:violation:gap-up-chase`를 fixture에서도
동일하게 재현했다. 실제 signal·Risk 호출·fill은 없다.

## ATR·stop 반사실

실제 경로는 gap 검사에서 끝나므로 authoritative 진입가·stop plan이 없다. 비교를
위해 gap gate만 없었다고 가정하면 09:05 완결봉 종가 108,700원이 실행 기준가,
5bp 불리한 가상 진입가는 108,754.35원이다. ATR14는 7,837.7825원, ATR 1.5배는
11,756.6738원, D-1 setup low까지 구조 거리는 12,400원이다. 큰 구조 거리를 써서
가상 stop은 96,300원이다. 자본·현금 100만원에서 Hermes 2% 정책은 1주, Rule
0.5% 정책은 0주다.

## 1분봉 경로와 추격 위험

- 09:01 고가 112,000원에서 09:03 저가 107,600원까지 2분 만에 -3.93%
- 09:05 가상 진입가 기준 09:30까지 MAE -0.33%, MFE +9.51%, 09:30 +7.86%
- 09:43 고가 기준 MFE +24.50%, 10:51 종가 124,600원 기준 +14.57%
- 09:43 고가에서 10:51까지 되밀림 -7.98%; 가상 stop 96,300원은 cutoff까지 미접촉

결과적으로 이 한 건은 거절 뒤 크게 상승한 false-negative 표본이다. 동시에 시초
고가 추격은 2분 안에 약 4% 흔들렸고 장중 고점 추격은 약 8% 되밀렸다. 따라서
사후 상승만으로 3% hard gate 완화를 정당화하지 않는다. 동일 fixture를 이후
gap threshold shadow 비교에 쓰되, 한 종목·당일 결과를 수익성 근거로 쓰지 않는다.

## 재현 한계

D-1 일봉은 조회 시점의 current-stored adjusted candle이다. 당시 immutable daily
snapshot 전체가 아니므로 exact historical engine replay가 아니라 저장 cycle 사유와
현재 보존 candle을 대조한 decision shadow다. 10:51 이후 경로는 이 기록에 포함하지
않는다.
