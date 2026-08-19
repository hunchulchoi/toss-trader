# RiskManager·자동화 감사 장부

Toss Trader는 PostgreSQL 사용 시 아래 장부를 운영 DB에 영구 저장한다.
SQLite 개발 모드에서도 같은 인터페이스와 필드를 제공한다.

| 장부 | 기록 내용 | 기록 시점 | 운영 조회 목적 |
|---|---|---|---|
| `paper_risk_decisions` | 승인·거부, 위반 규칙, 신호, 포지션, 가용 현금, 일일 수익률, Toss API 오류 연속 횟수, 장 상태, 판단 시각 | 신호별 RiskManager 판단 직후 | 체결 허용/차단 근거 확인 |
| `automation_run_logs` | daily/market scan과 n8n HTTP stage의 성공·실패·skip, `workflowId`, n8n `executionId`, trigger, portfolio/interval, 소요 시간, Hermes prompt/completion/total token, Telegram 결과, RiskManager `decision_id` 목록 | 각 automation/n8n 단계 완료 또는 실패 시 | execution 단위 흐름·token·Telegram 결과 사후 검토 |
| `paper_cycle_runs` | 장중·마감 cycle의 interval, 성공 여부, 신호·체결·실패 수, API 오류 연속 횟수, 일일 수익률 | 포트폴리오 cycle 시작·종료 시 | cycle 상태·실패 추이·성과 확인 |
| `paper_fills` | 승인 신호의 가상 체결, BUY/SELL, 수량·가격·금액·수수료·세금·근거·체결 시각 | RiskManager 승인 및 판단 장부 저장 후 | 이동평균 원가·실현손익·현금의 원천 장부. 실제 주문 내역 아님 |
| `paper_portfolio_daily_baselines` | 포트폴리오별 UTC 일자 시작 총자산 | 해당 일자 첫 손익 계산 시 | `daily_return_rate` 분모 고정 |
| `paper_portfolio_snapshots` | 총자산, 실현손익, 미실현손익, 누적 수수료·세금 | cycle 손익 계산 시. 체결 후 같은 시각 값 갱신 | cycle별 순손익·비용 추이 확인 |
| `dynamic_universe_runs` | 거래일 단위 종목군 생성 성공·실패, 후보·승인·선정 수 | 거래일 첫 universe 생성 시 | 후보 발굴 정상 여부와 규모 확인 |
| `dynamic_universe_decisions` | 거래대금 raw rank·정적 필터 뒤 eligible rank, 가격, membership 승인·거부, 위반 규칙, 최종 선정 여부 | 각 universe 후보 평가 시 | 왜 특정 종목이 선정/제외됐는지와 당시 순서 확인. metadata/config/candle snapshot이 없어 exact replay에는 사용 불가 |

RiskManager 판단은 paper fill보다 먼저 기록한다. 판단 기록에 실패하면 해당
paper fill도 실행하지 않는다. 실제 주문은 지원하지 않으며
`TRADING_ENABLED=false`를 유지한다.

Hermes 한도 거부: 로컬 preflight → `paper_risk_decisions` 1행. Hermes·n8n 없음, token 0.
통과 신호만 Hermes → n8n Risk 1회 → fill. Rule은 preflight 없이 n8n 1회.
n8n 장애는 `risk-manager-workflow-unavailable`. preflight 대상 아님.

최근 기록은 CLI에서 조회할 수 있다.

```bash
toss-trader risk-decisions --status rejected --limit 100
toss-trader risk-decisions --symbol 005930 --limit 20
toss-trader automation-runs --type market_scan --status failed --limit 100
```

## 실행·Grafana 조회 기준

| 구분 | 실제 흐름 | 확인 장부·패널 |
|---|---|---|
| legacy 장중 endpoint | `/run-paper-cycle`은 호환용 직접 endpoint이며 Hermes 비교 task를 호출하지 않음 | `paper_cycle_runs`, `paper_risk_decisions`, `paper_fills` |
| 운영 장중 workflow | `paper-rule-1m` → `paper-hermes-1m`. advisor는 신호+한도 통과 때만 | `Paper Cycle Run Log`, `Hermes Automation Run Log`, `n8n Flow Review Log` |
| 장전·마감 분석 | n8n이 Hermes API를 직접 호출하고, automation의 `hermes-*-result` endpoint가 응답·token을 검증·기록 | `automation_run_logs`의 `market_scan`·`daily`, token panel |
| RiskManager | universe는 로컬 정적 membership을 후보 장부에 저장. BUY·SELL 신호만 n8n 최종 Risk를 호출하며 승인된 trade만 fill 생성 | `Dynamic Universe Risk Decisions`, 최근 `paper_risk_decisions`, `Recent Paper Fills` |

Grafana `Toss Trader`: 장부 `toss-postgres`(ro), 상태 패널 `toss-prometheus`.
첫 화면은 상태·Rule vs Hermes·체결. 1분 차트·universe·자동화는 접힘.
Telegram 질의는 같은 paper 장부를 [`paper-mcp.md`](paper-mcp.md) 경로로 읽는다.
MCP holdings/pnl은 조회 시점 fills 재생이라 Grafana snapshot 패널과 다를 수 있다.

| 패널 | 표시 내용 | 데이터 기준 |
|---|---|---|
| `Rule vs Hermes 평가금액 · 손익` | 비용 반영 총자산과 시작현금 대비 손익(원) | `paper_portfolio_snapshots`, `paper_portfolios.initial_cash` |
| `n8n Flow Review Log` | 같은 execution의 stage, 소요시간, token, Telegram 결과, RiskManager decision ID | `automation_run_logs`의 workflow/execution metadata |
| `Dynamic Universe Risk Decisions` | 후보별 점수, RiskManager 판단, 최종 선정 여부 | `dynamic_universe_runs`, `dynamic_universe_decisions` |
| `Rule Trades (1m)` / `Hermes Trades (1m)` | 각 포트폴리오가 기간 내 체결한 종목 1분 등락률과 BUY/SELL 시점 | `market_candles`, `market_symbols`, `paper_fills` (`portfolio_id` 필터) |
| `Symbols (1m, BUY/SELL Marked · $trade_filter)` | 수집 1분봉의 조회 구간 시작 대비 정규화 등락률, 회사명·코드, BUY/SELL mark | `market_candles`, `market_symbols`, `paper_fills`; filter에 따라 체결 종목 또는 전체 조회 종목 |
| `Recent Paper Fills` | BUY/SELL, 수량·가격·금액, 수수료·세금. SELL은 이동평균 원가, 포지션 첫 매수시각, 해당 매도 실현손익 | `paper_fills` 이동평균 재생, `market_symbols` |
| `Paper Cycle Run Log` | rule/Hermes 포트폴리오별 cycle 상태, 신호·체결·실패·제외 수 | `paper_cycle_runs` |
| `Hermes Automation Run Log` | 장전·마감·실제 advisor token. 한도 preflight 거부는 없음 | `automation_run_logs`의 `market_scan`, `daily`, `hermes_trade` |

회사명은 `market_symbols` 조인. body·secret·전체 Hermes prompt/response 미저장.

candle 이력 부족과 setup-v2 완결 일봉 200 미달은 `skipped`. `failed_count`·API streak 불변. `partial_failure`는 종목 `error`. 합치지 말 것.
단일 통화 `daily_return_rate`는 UTC 일자 시작 총자산 대비 비용 반영 총자산
수익률이다. 다중통화는 환율 기준이 없어 통화별 MTM 수익률 중 최저값을 쓴다.
상세 계산은 [`pnl-engine.md`](pnl-engine.md)를 따른다.
한도 거부: `risk-decisions --status rejected`. advisor: `automation-runs --type hermes_trade`.
수동 마감·장애는 [`operations-runbook.md`](operations-runbook.md).

운영 안전 경계:

- Risk 판단을 paper fill보다 먼저 저장; 저장 실패 시 체결 금지
- 휴장일에는 매수·매도 모두 거부
- `TRADING_ENABLED=false` 고정, 실제 주문 코드 없음
- Grafana DB 사용자는 감사 장부 테이블, `market_candles`, `market_symbols` SELECT만 가능
