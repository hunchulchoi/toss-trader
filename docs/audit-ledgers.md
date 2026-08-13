# RiskManager·자동화 감사 장부

Toss Trader는 PostgreSQL 사용 시 아래 장부를 운영 DB에 영구 저장한다.
SQLite 개발 모드에서도 같은 인터페이스와 필드를 제공한다.

| 장부 | 기록 내용 | 기록 시점 | 운영 조회 목적 |
|---|---|---|---|
| `paper_risk_decisions` | 승인·거부, 위반 규칙, 신호, 포지션, 가용 현금, 일일 수익률, Toss API 오류 연속 횟수, 장 상태, 판단 시각 | 신호별 RiskManager 판단 직후 | 체결 허용/차단 근거 확인 |
| `automation_run_logs` | daily/market scan과 n8n HTTP stage의 성공·실패·skip, `workflowId`, n8n `executionId`, trigger, portfolio/interval, 소요 시간, Hermes prompt/completion/total token, Telegram 결과, RiskManager `decision_id` 목록 | 각 automation/n8n 단계 완료 또는 실패 시 | execution 단위 흐름·token·Telegram 결과 사후 검토 |
| `paper_cycle_runs` | 장중·마감 cycle의 interval, 성공 여부, 신호·체결·실패 수, API 오류 연속 횟수, 일일 수익률 | 포트폴리오 cycle 시작·종료 시 | cycle 상태·실패 추이·성과 확인 |
| `paper_fills` | 승인 신호의 가상 체결, BUY/SELL, 수량·가격·금액·근거·체결 시각 | RiskManager 승인 및 판단 장부 저장 후 | paper 포지션·현금·체결 근거 확인. 실제 주문 내역 아님 |
| `dynamic_universe_runs` | 30분 단위 동적 종목군 갱신 성공·실패, 후보·승인·선정 수 | universe refresh 시 | 후보 발굴 정상 여부와 규모 확인 |
| `dynamic_universe_decisions` | 후보별 랭킹 점수, 가격, RiskManager 승인·거부, 위반 규칙, 최종 선정 여부 | 각 universe 후보 평가 시 | 왜 특정 종목이 선정/제외됐는지 추적 |

RiskManager 판단은 paper fill보다 먼저 기록한다. 판단 기록에 실패하면 해당
paper fill도 실행하지 않는다. 실제 주문은 지원하지 않으며
`TRADING_ENABLED=false`를 유지한다.

운영 paper cycle의 trade·동적 universe RiskManager 요청은 인증된 n8n
sub-workflow를 거친다. n8n 장애 시 승인으로 우회하지 않고
`risk-manager-workflow-unavailable` 거부를 기록한다.

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
| 운영 장중 workflow | n8n이 `/workflow/paper-rule-1m` 뒤 `/workflow/paper-hermes-1m`을 호출. Hermes는 Hermes 포트폴리오에서 신호가 있을 때만 advisor로 호출 | `Paper Cycle Run Log`, `Hermes Automation Run Log`, `n8n Flow Review Log` |
| 장전·마감 분석 | n8n이 Hermes API를 직접 호출하고, automation의 `hermes-*-result` endpoint가 응답·token을 검증·기록 | `automation_run_logs`의 `market_scan`·`daily`, token panel |
| RiskManager | 신호 또는 universe 후보마다 승인/거부를 먼저 저장. 승인된 trade만 fill 생성 | `Dynamic Universe Risk Decisions`, 최근 `paper_risk_decisions`, `Recent Paper Fills` |

공용 Grafana `Toss Trader` dashboard는 장부·상세 패널에 `toss-postgres` read-only
datasource를, 상단 상태 패널(`Last Cycle`, `Daily Return`, `API Error Streak` 등)에
`toss-prometheus` datasource를 사용한다.

| 패널 | 표시 내용 | 데이터 기준 |
|---|---|---|
| `n8n Flow Review Log` | 같은 execution의 stage, 소요시간, token, Telegram 결과, RiskManager decision ID | `automation_run_logs`의 workflow/execution metadata |
| `Dynamic Universe Risk Decisions` | 후보별 점수, RiskManager 판단, 최종 선정 여부 | `dynamic_universe_runs`, `dynamic_universe_decisions` |
| `Symbols (1m, BUY/SELL Marked · $trade_filter)` | 수집 1분봉의 조회 구간 시작 대비 정규화 등락률, 회사명·코드, BUY/SELL mark | `market_candles`, `market_symbols`, `paper_fills`; filter에 따라 체결 종목 또는 전체 조회 종목 |
| `Recent Paper Fills` | BUY/SELL, 수량·가격·금액, 전략 근거 `reason` | `paper_fills`, `market_symbols` |
| `Paper Cycle Run Log` | rule/Hermes 포트폴리오별 cycle 상태, 신호·체결·실패·제외 수 | `paper_cycle_runs` |
| `Hermes Automation Run Log` | 장전·마감 분석 token 및 신호가 발생한 장중 Hermes advisor token | `automation_run_logs`의 `market_scan`, `daily`, `hermes_trade` |

장전 scan과 dynamic universe refresh는 Toss 종목 기본정보를 batch 조회해 회사명을
`market_symbols`에 갱신한다. Grafana는 이를 조인해 `회사명 (코드)`로 표시한다.
request body와 인증정보는 저장하지 않고 허용된 메타데이터와 집계값만 남긴다.
API key, bearer token, OAuth credential, 전체 Hermes prompt/response도 장부에
저장하지 않는다.

MA 계산에 필요한 candle 이력이 부족한 종목은 cycle 결과와
`automation_run_logs.details.skipped`에서 제외 수를 확인한다. 이 경우
`paper_cycle_runs.failed_count`와 `consecutive_api_errors`는 증가하지 않는다.
수동 마감 실행과 장애 대응 절차는
[`operations-runbook.md`](operations-runbook.md)를 따른다.

운영 안전 경계:

- Risk 판단을 paper fill보다 먼저 저장; 저장 실패 시 체결 금지
- 휴장일에는 매수·매도 모두 거부
- `TRADING_ENABLED=false` 고정, 실제 주문 코드 없음
- Grafana DB 사용자는 감사 장부 테이블, `market_candles`, `market_symbols` SELECT만 가능
