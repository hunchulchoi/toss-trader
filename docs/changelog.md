# Toss Trader 변경 이력

기능이 코드에 들어온 날짜다. 운영 반영은 항목에 따로 적는다.
매매 권고 아님.

## 2026-08-14

### 무신호 원인 (`idleReason`)

공용 Hermes가 `toss_paper_status`로 신호 수·체결만 보고 현금 대기를
자금 부족으로 오해하던 구멍을 막았다.

- `paper_cycle_runs.cycle_insight` JSON 저장
- 사이클 퍼널: scanned / evaluated / noCrossover / sellNoPosition /
  alreadyHeld / signals / riskRejected / fills
- 종목별 `symbolStates`: close, MA short/long, above|below
- status에 조회 시점 `cash`, `cashWeight`, `openPositionCount`
- 데드크로스인데 보유 없으면 `sell-no-position` (예전엔 신호만 버리고 끝)
- SOUL: `signals=0`이면 현금 탓하지 말고 `idleReason`을 읽는다

운영: 같은 날 오전 automation·paper-mcp 재빌드. 수동 1분 사이클 확인.
19종 스캔, 신호 0, `idleReason=no-crossover`. 삼성전자 1분 MA short < long.

상세: [`paper-mcp.md`](paper-mcp.md).

### 1분 trend continuation

1분봉 신규 골든크로스만 사면 이미 상승 정렬인 종목은 현금으로 남는다.
장중 1분 매수에 일봉 필터를 붙였다. 슬롯 5·수량 1주는 그대로다.

진입 (교차가 없을 때):

1. 일봉 `close > MA20 > MA60` 이고 20일 모멘텀 > 0 (`RISK_ON`)
2. 1분 `close > MA20 > MA60`
3. 그 종목을 아직 안 들고 있음
4. 종목당 한국 날짜 기준 하루 1회 (`…-cont-YYYY-MM-DD`)

이미 보유면 신호 폐기, `already-held`. 5종 한도면 신호는 나고
`max-open-positions`로 거부된다. 일봉 60개가 없으면 저장된 일봉을 쓰고,
모자라면 그때만 일봉 60개를 수집한다. 마감 일봉 사이클에는 이 경로 없음.

운영: 코드는 이 날짜에 들어갔다. continuation 이미지 배포는 changelog
작성 시점 기준 미완료. 다음 automation 재빌드 후 산다.
