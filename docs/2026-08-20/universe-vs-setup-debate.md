# 2026-08-20 대금 유니버스 vs setup-v2.2 신호 0

매매 권고 아님. `TRADING_ENABLED=false`. 장중 게이트 완화 없음.

질문: 12:00 KRX 전일 `ACC_TRDVAL` 15종 freeze 뒤 13:00 `signals=0`이
적정한가.

증거: `ranking_source=krx:acc-trdval` selected=15. rule/hermes
`setupV2Blocked=15`, `evaluated=0`. 15/15 `flow-not-confirmed`,
14/15 `missing-price-setup`. 403870만 가격 setup 통과.

## 3자 합의

**유지.** 버그 아님. 0-fill은 성공.

1. BUY 게이트 오늘 완화 금지.
2. Toss realtime 폴백 금지. 금액랭크 탈락 종목으로 15칸 채우기 금지.
3. 유니버스 정의를 당일 0건 때문에 바꾸지 않음. 8/19 성과 추격·A/B 오염.
4. 사냥터(전일 대금 상위) vs setup 밀도는 다일 shadow. live selector와 분리.
5. `evaluated=0`은 셋업 미채점이 아니라 BUY 게이트 전원 탈락.
6. KODEX 200 `RISK_OFF`는 스캔 레짐이지 setup-v2.2 게이트 아님.
7. 종목별 수급을 KRX 대금 API로 대체하지 않음 (ADR-005).

후속(코드 없음, 장후 읽기전용 가능): 적격 풀 hit-rate 관측, funnel 용어
문서화, 레짐 벤치마크 KRX 지수 조사는 유니버스/BUY와 분리.

역할 원문: Cursor 세션의 agy·Codex 역할 검토. 구현 없음.
