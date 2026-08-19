# PIT PostgreSQL migration

운영 공식 PIT 원장은 `common-postgres:5431`의 Toss Trader DB를 사용한다.
PostgreSQL 설정이 모두 주입되면 collector와 setup-v2 조회가 PostgreSQL을
선택하고, 설정이 없는 로컬 환경만 SQLite를 사용한다.

## Partition policy

- `market_universe_raw_v2`: `session_date` 월별 range partition
- `market_flow_pit_v2`: `session_date` 월별 range partition
- events, financials, valuation, coverage: 일반 테이블
- 애플리케이션이 새 월의 첫 행을 쓰기 전에 해당 월 파티션을 생성한다.
- 자동 drop은 없다. 과거 PIT는 백테스트와 의사결정 재현에 필요하다.

향후 보존기간을 적용할 때는 월 파티션을 먼저 archive/detach하고 원본 대비
행 수와 hash를 검증한다. 검증 전 drop은 금지한다.

## Legacy SQLite copy

Infisical machine identity로 PostgreSQL 설정을 주입한 컨테이너에서 실행한다.
복사는 `ON CONFLICT DO NOTHING`이라 재실행 가능하고 기존 PostgreSQL 행을
덮어쓰지 않는다.

```bash
toss-trader migrate-official-sqlite --source /app/data/market.db
```

전환 절차는 SQLite/PG 테이블별 행 수 대조, PostgreSQL 최근 KRX 행 확인,
collector와 automation 재생성, setup-v2 read 검증 순서다. 대조 실패 시 서비스는
기존 버전으로 유지한다.
