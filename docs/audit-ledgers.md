# RiskManager·자동화 감사 장부

Toss Trader는 PostgreSQL 사용 시 아래 장부를 운영 DB에 영구 저장한다.
SQLite 개발 모드에서도 같은 인터페이스와 필드를 제공한다.

- `paper_risk_decisions`: 승인·거부 여부, 위반 규칙, 신호, 포지션, 가용 현금,
  일일 수익률, Toss API 오류 연속 횟수, 장 상태와 판단 시각
- `automation_run_logs`: daily/market scan 성공·실패 단계, 소요 시간, Hermes
  prompt/completion/total token, 오류와 건수 요약
- `paper_cycle_runs`: 장중·마감 cycle의 interval, 성공 여부, 신호·체결·실패 수,
  API 오류 연속 횟수와 일일 수익률
- `paper_fills`: 승인된 신호의 가상 체결. 실제 주문 내역이 아님
- `dynamic_universe_runs`: 30분 단위 동적 종목군 갱신 성공·실패와 후보·승인·선정 수
- `dynamic_universe_decisions`: 후보별 랭킹 점수, 가격, RiskManager 승인·거부,
  위반 규칙과 최종 선정 여부

RiskManager 판단은 paper fill보다 먼저 기록한다. 판단 기록에 실패하면 해당
paper fill도 실행하지 않는다. 실제 주문은 지원하지 않으며
`TRADING_ENABLED=false`를 유지한다.

최근 기록은 CLI에서 조회할 수 있다.

```bash
toss-trader risk-decisions --status rejected --limit 100
toss-trader risk-decisions --symbol 005930 --limit 20
toss-trader automation-runs --type market_scan --status failed --limit 100
```

장중 `/run-paper-cycle`은 Hermes를 호출하지 않으므로 `automation_run_logs`에
token 행을 만들지 않는다. 실행 상태는 `paper_cycle_runs`, 신호가 발생한 경우
Risk 판단은 `paper_risk_decisions`, 승인된 가상 체결은 `paper_fills`에서 본다.

공용 Grafana의 `Toss Trader` dashboard는 `toss-postgres` read-only datasource로
최근 판단, paper cycle 실행, 가상 체결, Hermes token 사용량과 자동화 실행
로그를 조회한다. `Dynamic Universe Risk Decisions`는 후보별 RiskManager 판단과
선정 결과를 표시한다. `Queried Symbols`는 수집한 1분봉을 조회 구간 시작 대비
등락률로 정규화해 15개 심볼을 함께 표시한다. 장전 scan은 Toss
`GET /api/v1/stocks`를 한 번 호출해 회사명을 `market_symbols` 기준정보 테이블에
갱신하고, Grafana의 종목 표시는 `회사명 (코드)` 형식으로 이 테이블을 조회한다.
`Recent Paper Fills`는 BUY/SELL, 수량·가격·금액과 전략 근거 `reason`을 함께
표시한다. `Paper Cycle Run Log`는 장중
5분마다 갱신되고 `Hermes Automation Run Log`는 Hermes를 호출하는 장전·마감
실행에만 행이 추가된다.
장부에는 API key, bearer token, 전체 Hermes prompt/response를 저장하지 않는다.

운영 안전 경계:

- Risk 판단을 paper fill보다 먼저 저장; 저장 실패 시 체결 금지
- 휴장일에는 매수·매도 모두 거부
- `TRADING_ENABLED=false` 고정, 실제 주문 코드 없음
- Grafana DB 사용자는 감사 장부 테이블, `market_candles`, `market_symbols` SELECT만 가능
