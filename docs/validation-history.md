# Toss Trader 운영 검증 이력

## 2026-08-13 마감 검증

| 항목 | 결과 |
|---|---|
| n8n execution | `224`, `success` |
| 소요 시간 | 약 11.6초 |
| rule | 17종목, 신호 0, 체결 0, 제외 1, 실패 0 |
| Hermes | 17종목, 신호 0, 체결 0, 제외 1, 실패 0 |
| 제외 | `487400`, 일봉 1개로 MA20/MA60 이력 61개 미달 |
| rule 일일 수익률 | `+2.8425%` |
| Hermes 일일 수익률 | `+3.7269%` |
| Hermes token | prompt 5,293 / completion 189 / total 5,482 |
| Telegram | accepted, Alertmanager Telegram failure counter 0 |
| Toss API 연속 오류 | 0 |
| PostgreSQL lock·idle transaction | 0 |
| trading | `TRADING_ENABLED=false` |

수익률은 보유 전체 MTM. 오늘 체결 성과 아님. 매매 권고 아님.
