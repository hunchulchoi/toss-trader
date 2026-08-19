# KRX investor-flow CSV import

KRX 정보데이터시스템의 전일 확정치를 setup-v2 수급 원장에 넣는 수동 공식
경로다. 자동 웹 스크래핑은 KRX의 `LOGOUT` 차단 이력이 있어 사용하지 않는다.

## 입력 계약

- 같은 세션·같은 시장 범위의 `외국인합계`, `기관합계` CSV 두 개
- DataGo 원장에 해당 세션이 아직 없으면 KRX `전종목 시세` 거래대금 CSV
- 필수 열: `종목코드`, `거래대금_순매수` (`순매수거래대금`,
  `순매수대금`도 허용)
- UTF-8/CP949, comma/tab/semicolon 구분 지원
- 두 파일에 모두 있는 현재 `market_symbols`의 6자리 국내 종목만 적재
- 해당 세션이 공식 universe 원장에 있고 종목별 거래대금이 양수여야 함

DataGo 공식 원장이 전일 세션을 아직 게시하지 않았다면 마지막 원장 세션부터
import 날짜까지 Toss 한국장 캘린더를 확인해 session index를 계산한다. import
날짜가 휴장이거나 이전 공식 세션이 없으면 거부한다.

헤더·세션·거래대금 검증이 실패하면 행을 쓰지 않는다. 투자자 거래가 없어 한쪽
CSV에서 빠진 종목은 적재하지 않고 해당 종목만 setup-v2에서 계속 fail-closed한다.
첫 import는
`market_flow_pit_v2`에 `source=krx:manual-csv`로 저장하고
두 CSV가 현재 유니버스를 완전히 포함할 때만
`market_pit_coverage(dataset=flow_krx)`를 기록한다. 같은 소스·세션 재실행은
원본 관측값과 `available_at`을 바꾸지 않는다.

운영에서는 `common-postgres`의 월별 `session_date` 파티션에 저장한다. PostgreSQL
설정이 없는 로컬 개발 환경만 `MARKET_DB_PATH` SQLite로 돌아간다. 과거 파티션은
백테스트·감사·정정 재현 자료이므로 자동 삭제하지 않는다. 보존기간이 확정되면
archive와 행 수·hash 검증 후 월 파티션 단위로 detach/drop한다.

## 실행

프로젝트 `AGENTS.md`의 machine identity 메모리 토큰 규칙으로 Infisical을
주입한 뒤 실행한다.

```bash
toss-trader import-krx-flow-csv \
  --session-date 2026-08-18 \
  --foreign-csv /path/to/foreign.csv \
  --institutional-csv /path/to/institutional.csv \
  --trading-csv /path/to/all-stocks.csv
```

KIS와 KRX가 같은 종목·세션에 공존하면 setup-v2 조회는 KRX를 우선하되,
의사결정 시점보다 늦게 관측된 KRX 행은 보이지 않아 당시 사용 가능했던 KIS로
fail-safe하게 돌아간다. 한 세션은 소스 수와 무관하게 한 번만 센다.
