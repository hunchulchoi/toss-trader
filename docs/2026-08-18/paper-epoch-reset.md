# Rule/Hermes paper 실험 세대 전환

## 목적

2026-08-19부터 Rule과 Hermes를 동일한 시작 조건으로 다시 비교한다. 기존
MA 기반 체결을 setup-v2.2 결과와 섞지 않는다.

## 실행

- 전환 시각: 2026-08-18 15:43 KST
- 사전 조건: 15:40 Rule/Hermes cycle, daily report, Telegram report 모두 성공
- 복구 dump: `data/paper-ledger-pre-reset-20260818T063502Z.dump`
- 기존 Rule 세대: `rule-v1-20260818`
- 기존 Hermes 세대: `hermes-v1-20260818`
- 신규 활성 세대: `rule`, `hermes`
- 신규 시작시각: 2026-08-19 00:00 KST
- 신규 초기현금: 각각 1,000,000원
- 신규 체결·보유·리스크결정·스냅샷·baseline·v2 plan·cycle: 0

기존 자료를 삭제하지 않았다. 포트폴리오 ID를 archive ID로 원자적으로
이동했다. 보존량은 체결 43건, 리스크결정 605건, 스냅샷 456건, 일별 baseline
6건, cycle 538건이다. 기존 v2 position plan은 0건이었다.

## 비교 규율

- 수수료·세금·슬리피지·유니버스·리스크 한도는 양쪽 동일
- Rule/Hermes 판단 차이만 비교
- 장중 규칙 변경 금지
- KIS 수급 6개 완료 세션 전 신규 BUY 0건은 정상 fail-closed
- 과거 archive와 신규 활성 세대 손익을 합산하지 않음
- `TRADING_ENABLED=false` 유지

## 검증

- 활성 두 포트폴리오: 초기현금 1,000,000원, 체결 0, 순보유 0
- 활성 종속 테이블 5종과 cycle 테이블: 모두 0건
- archive 포트폴리오 2개 및 기존 체결 건수 재조회 성공
- paper MCP health: `ok`
