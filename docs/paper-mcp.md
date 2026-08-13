# Hermes Telegram paper MCP

Hermes Telegram은 내부 HTTP MCP `toss-paper`를 통해 Rule/Hermes paper 장부만
조회한다. `hermes-analysis` sidecar에는 MCP나 다른 tool을 연결하지 않는다.

## 도구

| tool | 범위 |
|---|---|
| `toss_paper_status` | Rule/Hermes 마지막 cycle, 최근 Hermes 호출, 실패 |
| `toss_paper_holdings` | paper 보유 수량, 평균원가, 최신 평가금액 |
| `toss_paper_pnl` | 총자산, 실현·미실현손익, 비용, 시작현금 대비 손익 |

세 도구 모두 입력 인자가 없고 고정 SELECT만 실행한다. 임의 SQL, 주문, 설정 변경,
Toss API 호출 기능은 없다. CLI의 `holdings`는 실계좌 조회이므로 MCP에서 노출하지
않는다.

## 격리

- `paper-mcp` 컨테이너에는 Toss client ID, secret, 계좌번호를 주입하지 않는다.
- PostgreSQL 세션은 연결 시 `default_transaction_read_only=on`으로 고정한다.
- 운영 Toss DB `5431`의 `rule`, `hermes` 장부만 조회한다.
- MCP port `8090`은 publish하지 않고 `openclaw-net` 내부에만 expose한다.
- 컨테이너는 read-only root filesystem, 전체 capability drop으로 실행한다.
- `automation/hermes-analysis/config.yaml`의 `mcp_servers: {}`는 유지한다.

## 배포와 Hermes 등록

Infisical 환경으로 MCP만 재생성한다.

```bash
infisical run --env=prod --path=/ -- \
  docker compose up -d --build paper-mcp
```

Telegram Hermes 컨테이너에 내부 endpoint를 등록하고 세 도구를 Telegram에
활성화한다.

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
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/healthz').read().decode())"
```
