# Toss Trader 용어집 (Glossary)

이 문서는 Toss Trader 시스템, 전략(`setup-v2.3`), 데이터 파이프라인, 운영 문서([`docs/`](.)) 전반에서 사용되는 핵심 용어와 개념을 정리한 공식 용어집입니다.

---

## 목차
1. [시스템 및 아키텍처 안전 (Architecture & Safety)](#1-시스템-및-아키텍처-안전-architecture--safety)
2. [데이터 및 시점 무결성 (Data & Point-In-Time)](#2-데이터-및-시점-무결성-data--point-in-time)
3. [전략 및 매매 규칙 (Strategy & Rules - setup-v2.3)](#3-전략-및-매매-규칙-strategy--rules---setup-v23)
4. [원장 및 손익 엔진 (Ledgers & PnL Engine)](#4-원장-및-손익-엔진-ledgers--pnl-engine)
5. [자동화 및 외부 연동 (Automation & Interfaces)](#5-자동화-및-외부-연동-automation--interfaces)
6. [멀티 에이전트 협업 체계 (Multi-Agent System)](#6-멀티-에이전트-협업-체계-multi-agent-system)

---

## 1. 시스템 및 아키텍처 안전 (Architecture & Safety)

### Fail-Closed (페일 클로즈드)
- **정의**: 데이터가 누락되거나, 검증되지 않았거나, 시스템 장애 또는 불확실성이 발생했을 때 위험을 회피하기 위해 **"진입/매수를 무조건 차단(거부)"**하는 안전 최우선 원칙.
- **적용**: 수급 6세션 미달, 캘린더/공시 미수집, 200봉 부족, 랭킹 실패 시 신규 매수를 0건으로 처리하고 보유 종목의 청산(SELL) 경로만 유지.
- **관련 문서**: [ADR-005](decisions.md#adr-005-official-pit-inputs-remain-fail-closed), [paper-cycle-flow.md](paper-cycle-flow.md)

### TRADING_ENABLED (안전 킬 스위치)
- **정의**: 실계좌 주문 실행을 허용할지 결정하는 전역 환경 변수.
- **기본값**: `false` (현재 시스템은 가상 매매인 Paper Trading 전용이며, 실주문 발주 코드가 격리/차단되어 있음).
- **관련 문서**: [README.md](../README.md#안전-기본값), [AGENTS.md](../AGENTS.md#production-safety)

### Infisical & Universal Auth (비밀값 관리)
- **정의**: API 키, DB 패스워드 등 민감한 자격증명을 `.env`나 코드에 저장하지 않고 Infisical 시크릿 매니저를 통해 프로세스 실행 시 동적으로 주입하는 체계.
- **보안 규칙**: Universal Auth 로그인 시 발급되는 토큰은 터미널·로그·인자에 노출되지 않도록 서브쉘 내부 메모리 변수로만 캡처하여 사용 후 즉시 파기.
- **관련 문서**: [AGENTS.md](../AGENTS.md#production-safety)

### Shared Snapshot (공유 시장 스냅샷)
- **정의**: 동일 시점에 수집된 시장 데이터(종목, 분봉, 일봉 200개, 수급, 공시)를 Rule 기반 엔진과 Hermes LLM 엔진이 동일하게 공유하여 사용하는 메커니즘.
- **목적**: 두 포트폴리오의 성과 차이가 시장 데이터 수집 시점 차이가 아닌 "LLM Advisor 개입 여부 및 리스크 관리"에서만 발생하도록 통제.
- **관련 문서**: [paper-cycle-flow.md](paper-cycle-flow.md#universe-and-market-snapshot)

---

## 2. 데이터 및 시점 무결성 (Data & Point-In-Time)

### PIT (Point-In-Time, 시점 무결성)
- **정의**: 과거 특정 시점의 전략을 평가하거나 백테스트할 때, **"당시 시점에 실제로 관측 가능했던 데이터"만 사용**하고 미래 데이터를 참조(Look-ahead Bias)하지 않도록 보장하는 원칙.
- **관련 문서**: [ADR-005](decisions.md#adr-005-official-pit-inputs-remain-fail-closed), [docs/2026-08-18/pit-collection.md](2026-08-18/pit-collection.md)

### `available_at` / `retrieved_at` / `session_date`
- **`session_date`**: 거래가 일어난 시장 거래일(기준일자).
- **`retrieved_at`**: 데이터 수집기가 API나 소스에서 해당 데이터를 실제로 응답받은 시각.
- **`available_at`**: 전략 엔진이 해당 데이터를 공식적으로 의사결정에 사용할 수 있게 된 최초 유효 시각 (`decision_at >= available_at` 조건 만족 시에만 사용 가능).

### First-Observed Principle (최초 관측 원칙)
- **정의**: 과거 수급이나 공시 데이터를 나중에 조회하더라도, 해당 데이터의 가용 시점(`available_at`)은 **"실제 최초 조회된 시각"으로 고정**하고 과거 시점으로 소급하지 않는 원칙.
- **관련 문서**: [docs/2026-08-18/pit-collection.md](2026-08-18/pit-collection.md#flow-source-and-pit-contract)

### 수급 데이터 소스 우선순위 (Data Source Priority)
- **우선순위**: 동일 세션(`session_date`)에 대해 **KRX 공식 CSV (`import-krx-flow-csv`) > KIS Open API (`FHPTJ04160001`)**.
- **이유**: KRX 공식 확정치 원장을 최우선으로 신뢰하며, 실시간/당일 배치는 KIS first-observed를 사용.
- **관련 문서**: [docs/2026-08-19/krx-flow-import.md](2026-08-19/krx-flow-import.md)

### 주요 PIT 테이블
- **`market_flow_pit_v2`**: 종목별 외인(`frgn_ntby_tr_pbmn`), 기관(`orgn_ntby_tr_pbmn`) 순매수대금 및 누적 거래대금 원장.
- **`market_events_pit_v2`**: OpenDART 공시 사실과 매수 진입 차단 윈도우(`blocked_through`).
- **`market_pit_coverage`**: 날짜별 소스 수집 성공 여부 및 무사건(Zero-event) 확인 체크포인트.
- **관련 문서**: [docs/2026-08-18/pit-collection.md](2026-08-18/pit-collection.md#runtime-tables)

---

## 3. 전략 및 매매 규칙 (Strategy & Rules - setup-v2.3)

### setup-v2.3
- **정의**: 완결 일봉 200개 기반 가격 셋업, 6세션 PIT 수급 이력과 반전 가점, OpenDART 공시 차단 필터, D+1 첫 1분봉 3% 갭 검사를 결합한 한국 주식 paper 전략.
- **기존 MA 전략과의 차이**: 단순 MA 골든크로스로 신호를 만들지 않고, 200일 완결 데이터와 엄격한 사전 게이트를 통과한 후보만 생성.
- **관련 문서**: [docs/2026-08-14/setup-v2-design.md](2026-08-14/setup-v2-design.md), [docs/2026-08-18/setup-v2-activation.md](2026-08-18/setup-v2-activation.md)

### Price Setup (가격 셋업)
- **조건**: 다음 두 가지 중 하나를 만족해야 함:
  1. **Pullback (눌림목)**: `MA50 > MA200` (장기 상승 추세) 상태에서 주가가 MA50 부근으로 단기 조정 후 지지.
  2. **Oversold Reversal (과매도 반전)**: 과매도 구간(RSI14 등) 이후 반등 전환 확인.
- **차단 조건**: RSI 과열, 급락 중인 낙하 칼날(Falling knife), 장기 하락 추세.

### Flow Reversal (수급 전환 / 6세션 룰)
- **정의**: 의사결정 시점에 관측 완료된 **연속 6개 세션**의 수급 데이터를 검사.
- **규칙**: 연속 6세션 이력은 필수다. 이전 5개 세션 외국인 누적 순매수 비율이 음수였다가 최신 외인 순매수가 양수로 전환하면 가점하며, 미반전만으로 BUY를 차단하지 않는다. 기관 순매수 확인도 가산 요인이다.
- **관련 문서**: [docs/2026-08-14/setup-v2-design.md](2026-08-14/setup-v2-design.md)

### D+1 Entry & 3% Gap Filter (D+1 진입 및 갭 검사)
- **동작**: 전일(D) 완결 데이터로 승인된 후보에 대해, 익일(D+1) 정규장 첫 1분봉이 완성되는 시점에 시가 대비 갭 상승/하락 폭을 재검사.
- **규칙**: 전일 종가 대비 당일 시초 갭이 **+3.0% 초과**하여 급등 출발하면 추격 매수를 방지하기 위해 매수를 즉시 차단(`setup-v2-block`).
- **관련 문서**: [paper-cycle-flow.md](paper-cycle-flow.md#d1-entry-and-sizing)

### Dynamic Universe (동적 유니버스)
- **정의**: 고정된 종목군 대신, 서울 거래일 오전에 Toss 실시간 거래대금, 12:00
  KST부터 KRX 전일 `ACC_TRDVAL` 상위 종목군을 바탕으로 적격 보통주를 랭크하여
  선정하는 유니버스.
- **규칙**: 직전 완결 200봉이 있는 적격 보통주만 최대 15개까지 당일 유니버스로 동결. 가격 셋업은 BUY 게이트. 선정 0종은 freeze하지 않음. 기존 보유 종목은 유니버스 밖으로 밀려나도 청산 추적을 유지.
- **관련 문서**: [docs/2026-08-19/universe-strategy-debate.md](2026-08-19/universe-strategy-debate.md), [README.md](../README.md#실행)

### Position Sizing (포지션 사이징)
- **기본 원칙**: 총자산 대비 단일 거래 리스크 한도(0.5%), 동시 보유 한도(최대 10종목), 일일 최대 매수 횟수(5회), ATR14 기반 손절폭 계산.
- **정수 주식 수**: 최소 1주 단위 계산 후 현금 잔고 및 종목별 한도 내 절사.

---

## 4. 원장 및 손익 엔진 (Ledgers & PnL Engine)

### Dual Portfolio (Rule vs Hermes 듀얼 포트폴리오)
- **Rule Portfolio (`rule`)**: 순수 정량적 룰(setup-v2.3 + RiskManager)에 따라 자동으로 체결되는 가상 포트폴리오.
- **Hermes Portfolio (`hermes`)**: 동일한 시장 스냅샷과 룰을 거친 신호에 대해 Hermes LLM Advisor의 추가 분석/승인을 거치는 가상 포트폴리오.
- **특징**: 각각 1,000,000원의 독립된 초기 현금과 장부를 가지며 상호 간섭 없음.
- **관련 문서**: [pnl-engine.md](pnl-engine.md), [paper-cycle-flow.md](paper-cycle-flow.md)

### Paper Ledgers (가상 매매 원장 테이블)
- **`paper_orders`**: 가상 주문 생성 및 상태 기록.
- **`paper_fills`**: 체결 기록 (체결가, 수량, 수수료, 세금).
- **`paper_positions`**: 종목별 평균단가, 보유수량, 실현손익.
- **`paper_cash`**: 포트폴리오별 현금 입출금 및 잔액.
- **관련 문서**: [pnl-engine.md](pnl-engine.md)

### PnL Engine & 비용 모델
- **수수료 및 세금**: 2026-08 토스증권 일반 요율 적용.
  - 국내 보통주(6자리 심볼): 매수·매도 수수료 **0.015%**, 매도 거래세 **0.20%** (원 미만 절사).
  - 미국 주식: 수수료 **0.1%** ($10 이하 주문 면제).
- **평가 및 손익**: 최신 체결가/종가 기준 미실현 손익 및 포트폴리오 총자산(NAV)을 실시간 산출.
- **관련 문서**: [pnl-engine.md](pnl-engine.md)

### Audit Ledgers (감사 원장 테이블)
- **`paper_cycle_runs`**: 5분마다 실행된 사이클의 상태, 신호 건수, 체결 건수, 펀넬 메트릭 저장.
- **`risk_decisions`**: RiskManager의 종목별 승인/거부 판정과 상세 차단 사유 기록.
- **`hermes_token_audits`**: Hermes LLM 호출 시 소비된 입력/출력 토큰 및 응답 시간 감사.
- **관련 문서**: [audit-ledgers.md](audit-ledgers.md)

### Epoch Reset (장부 세대 초기화)
- **정의**: 전략 버전 업그레이드나 대규모 구조 변경 시, 과거 가상 체결 이력을 보존한 채 신규 세대(`epoch`)를 열어 초기 현금부터 새롭게 시작하는 기능.
- **관련 문서**: [docs/2026-08-18/paper-epoch-reset.md](2026-08-18/paper-epoch-reset.md)

---

## 5. 자동화 및 외부 연동 (Automation & Interfaces)

### n8n Workflows
- **역할**: 전체 스케줄링, 단계별 API 호출, 에러 핸들링, Telegram 알림 오케스트레이션.
- **주요 워크플로우**:
  1. `toss-trader-market-scan`: 평일 08:30 KST 장전 시장 레짐 분석 및 종목 발굴.
  2. `toss-trader-intraday-paper`: 평일 09:00~15:20 KST (5분 간격) Rule/Hermes 장중 사이클 실행.
  3. `toss-trader-daily`: 한국장 영업일 11:50 KST 비확정 중간 분석과
     15:40 KST 일봉 마감 분석 및 일일 성과 요약.
  4. `toss-trader-error`: 장애 발생 시 Alertmanager 즉시 통보.
- **관련 문서**: [automatic-trading-scenario.md](automatic-trading-scenario.md), [system-workflow.md](system-workflow.md)

### Hermes LLM Advisor
- **정의**: `hermes-analysis` 모델을 사용하여 장전 브리핑 작성, 장중 진입 신호 2차 검토, 장 마감 일일 요약을 생성하는 AI 분석 컴포넌트.
- **Hard Preflight**: 필수 데이터·이벤트·갭·수량·현금·시간·Risk를 통과하지
  못한 신호는 LLM 호출 없이 차단. Hermes experimental paper에서는 가격
  셋업·RSI·낙하 칼날 판정만 advisor 참고 근거로 사용.
- **관련 문서**: [automatic-trading-scenario.md](automatic-trading-scenario.md#hermes-연동)

### Paper MCP (Model Context Protocol)
- **정의**: Telegram의 Hermes 어시스턴트가 사용자의 자연어 요청에 따라 포트폴리오 잔고, 포지션, 체결 내역, 차단 사유를 조회할 수 있도록 제공하는 읽기 전용 도구 인터페이스.
- **관련 문서**: [paper-mcp.md](paper-mcp.md)

### Timeline Web UI
- **정의**: 포트폴리오 자산 추이, 보유 종목, 최근 63개 시세 스파크라인, 5분 사이클별 상태기계 펀넬을 시각화하는 읽기 전용 웹 대시보드 (`/cycles`). `/cycles` 시각은 서울 24시, 기본 날짜는 서울 달력 오늘.
- **관련 문서**: [README.md](../README.md#실행), [operations-runbook.md](operations-runbook.md)

### Alertmanager & Telegram
- **정의**: 사이클 내 체결, 거부, API 오류, 수집 장애 발생 시 Telegram 특정 토픽으로 구조화된 알림을 발송하는 모니터링 파이프라인.
- **관련 문서**: [system-workflow.md](system-workflow.md), [operations-runbook.md](operations-runbook.md)

---

## 6. 멀티 에이전트 협업 체계 (Multi-Agent System)

### Agent Roles (에이전트 역할 분담)
- **Codex (Builder)**: 백엔드 구현, DB/마이그레이션, 전략 리팩토링, 단위 테스트, 버그 수정 담당 (`agent/codex` 브랜치).
- **Cursor (Reviewer & UI)**: 프론트엔드/UI, 타임라인 웹, 코드 리뷰, 통합 검증 담당 (`agent/cursor` 브랜치).
- **agy (Researcher)**: 전략 가설 검증, 백테스트, 외부 API 및 아키텍처 조사 담당 (`agent/agy` 브랜치).
- **관련 문서**: [AGENTS.md](../AGENTS.md#roles), [decisions.md](decisions.md#adr-001-agent-worktree-isolation)

### Worktree & Tasks Board
- **규칙**: 에이전트는 각자의 전용 워크트리에서 작업하며, 공통 작업 현황 및 소유권은 `main` 브랜치의 [`docs/tasks.md`](tasks.md)를 통해 관리.
- **관련 문서**: [AGENTS.md](../AGENTS.md#git), [tasks.md](tasks.md)
