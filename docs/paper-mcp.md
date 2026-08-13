# Hermes Telegram paper MCP

공용 Hermes Telegram에서 paper 자동매매 상태를 물을 때 내부 HTTP MCP
`toss-paper`를 쓴다. 예: 지금 어떻게 되나, 보유 종목이 뭐냐, 수익이 얼마냐.

분석 sidecar `hermes-analysis`에는 MCP나 다른 tool을 연결하지 않는다.
`automation/hermes-analysis/config.yaml`의 `mcp_servers: {}`는 유지한다.

Alertmanager Telegram은 리포트·장애를 **밀어 넣는** 경로다. 이 MCP는 운영자가
공용 Hermes에 **물어보는** 경로다. 둘은 같은 paper 장부를 읽지만 봇이 다르다.

## 도구

세 도구 모두 입력 인자가 없다. 호출마다 `rule`과 `hermes` 장부를 함께 반환한다.

| tool | 질문에 답 | 읽기 원천 |
|---|---|---|
| `toss_paper_status` | 자동매매가 지금 어떻게 되나 | 포트폴리오별 마지막 `paper_cycle_runs`, 최근 Hermes 호출(`hermes_trade` 또는 stage `hermes-analysis`), 최근 실패 최대 10건 |
| `toss_paper_holdings` | 지금 보유 종목이 뭐냐 | `paper_fills` 이동평균 재생 + 최신 캔들 평가. 수량 0은 제외 |
| `toss_paper_pnl` | 수익이 얼마냐 | 같은 재생. 현금, 평가금액, 총자산, 실현·미실현, 누적 수수료·세금, 시작현금 대비 손익 |

임의 SQL, 주문, 설정 변경, cycle 실행, Toss API 호출은 없다. CLI `holdings`는
실계좌 조회이므로 MCP에 노출하지 않는다.

손익 계산은 [`pnl-engine.md`](pnl-engine.md)와 같다. MCP는
`paper_portfolio_snapshots`를 읽지 않고 조회 시점에 fills를 재생한다. Grafana
스냅샷 패널은 마지막 cycle 기록이라 그 사이 시세만 바뀌면 숫자가 다를 수 있다.

## 격리

- `paper-mcp` 컨테이너에는 Toss client ID, secret, 계좌번호를 주입하지 않는다.
- DB 로그인은 전용 `toss_mcp_reader`를 사용하며 모든 public table에 `SELECT`만
  허용한다. 새 테이블에도 default privilege로 `SELECT`만 상속한다.
- PostgreSQL 세션은 `default_transaction_read_only=on`, statement/idle timeout 5초다.
- 운영 Toss DB는 `common-postgres` 호스트 포트 `5431`의 `rule`, `hermes`만 본다.
- MCP port `8090`은 publish하지 않고 `openclaw-net` 내부 alias
  `toss-trader-paper-mcp`로만 expose한다.
- 컨테이너는 read-only root filesystem, 전체 capability drop으로 실행한다.

## 배포와 Hermes 등록

Infisical `prod` `/`에 전용 자격증명을 저장한다.

```dotenv
TOSS_MCP_POSTGRES_USER=toss_mcp_reader
TOSS_MCP_POSTGRES_PASSWORD=<random secret>
```

role migration은 비밀번호를 출력하거나 shell argument에 넣지 않는다.

```bash
docker cp db/paper_mcp_reader.sql \
  common-postgres:/tmp/toss_trader_paper_mcp_reader.sql

infisical run --env=prod --path=/ -- \
  docker exec -e TOSS_MCP_POSTGRES_PASSWORD common-postgres \
  psql -U postgres -d toss_trader \
  -f /tmp/toss_trader_paper_mcp_reader.sql
```

Infisical 환경으로 MCP만 재생성한다.

```bash
infisical run --env=prod --path=/ -- \
  docker compose -p toss-trader up -d --build paper-mcp
```

공용 Hermes 컨테이너에 내부 endpoint를 등록하고 세 도구를 Telegram에
활성화한다. 분석 sidecar `hermes-analysis`에는 등록하지 않는다.

```bash
docker exec hermes hermes mcp add toss-paper \
  --url http://toss-trader-paper-mcp:8090/mcp

docker exec hermes hermes tools enable --platform telegram \
  toss-paper:toss_paper_status \
  toss-paper:toss_paper_holdings \
  toss-paper:toss_paper_pnl
```

확인:

```bash
docker exec hermes hermes mcp test toss-paper
docker exec hermes hermes tools list --platform telegram
docker exec toss-trader-paper-mcp-1 \
  python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8090/healthz')))"
```

healthz 기대값: `{"status": "ok", "tools": 3}`.
