"""Run the daily analysis panel inside the main Hermes container."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUTOMATION_URL = os.environ.get(
    "TOSS_AUTOMATION_URL", "http://toss-trader-automation:8088"
).rstrip("/")
CURSOR_AGENT = os.environ.get("CURSOR_AGENT_BIN", "/usr/local/bin/cursor-agent")
HERMES = os.environ.get("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
PAPER_MCP_URL = os.environ.get(
    "TOSS_PAPER_MCP_URL", "http://toss-trader-paper-mcp:8090/panel-mcp"
)

ROLES = {
    "gpt": {
        "role": "quant analyst",
        "model": "cursor-grok-4.6-high-fast",
        "instruction": (
            "수익률·체결·신호·후보 funnel을 정량 분석하라. 비교 가능한 수치와 "
            "데이터 한계를 구분하고, 제공되지 않은 값을 추정하지 마라. 오늘 근거로 "
            "검증 가능한 개선 가설과 측정 지표를 제안하라."
        ),
    },
    "grok": {
        "role": "skeptic / anomaly detector",
        "model": "cursor-grok-4.6-high-fast",
        "instruction": (
            "이상치·장부 불일치·PIT/미래참조·데이터 누락·운영 실패 가능성을 "
            "공격적으로 찾되, JSON에 없는 사실은 만들지 마라. 다른 개선안의 "
            "hindsight·표본편향·부작용과 반증 조건을 찾아라."
        ),
    },
    "gemini": {
        "role": "Risk Manager",
        "model": "gemini-3.7-flash-high",
        "instruction": (
            "노출·현금·손실·거절 사유·체결 및 시스템 위험을 평가하라. "
            "universe membership과 주문 실행 Risk를 혼동하지 마라. 개선안의 "
            "shadow/paper 경계, 중단 조건과 롤백 가능성을 판정하라."
        ),
    },
}


MARKET_CRITIQUE = (
    "marketContext가 있으면 스킵/유휴 사유를 벤치마크·감시종목 vsOpen/vsPrevClose와 "
    "먼저 대조하라. 퍼널 코드만 반복하는 전략 토론은 금지. changedFacts가 비어도 "
    "시장 대비 괴리를 짧게 적어라. JSON에 없는 뉴스·사후 매수 기회는 만들지 마라. "
    "cycle funnel의 dailyCandidates는 해당 portfolio가 arm 가능한 D-1 일봉 후보 수다. "
    "Rule에서는 정식 승인 후보다. evaluated는 대기·차단이 "
    "없는 종목 수라 후보 수로 쓰지 마라. openingBarPending은 승인 후보가 첫 완결 "
    "1분봉을 기다리는 정상 상태이며 0후보나 setup 차단으로 해석하지 마라. "
    "universe.runId=null 이고 refreshed=false면 당일 freeze cacheHit이지 데이터 오류가 아니다. "
    "1d cycle의 intradaySample.applicable=false는 설계다. marketContext와 "
    "intradayReview.reasonPath/armRejectDetail을 써라. below-one-lot은 사이징 0주 사실이지 "
    "전략 무효 선언이 아니다. 무체결·무오류만으로 리스크 해소를 말하지 마라. "
    "sessionAccountingV1이 있으면 paper 체결·보유·dailyBaselineEquity의 authoritative "
    "source다. summary.fillScope=current-cycle과 intradayReview.fillScope="
    "seoul-session-cumulative를 구분하라. sessionAccountingV1의 수치와 두 scope가 "
    "일치하면 체결 집계 충돌로 보고하지 마라. 없거나 실제 불일치할 때만 evidence "
    "도구로 원장을 확인하라. "
    "eventGateShadow.status=expired-unresolved면 authoritative 차단은 유지된 상태다. "
    "eventFamily·blockedThrough·wouldRuleApproveWithoutEvent·"
    "wouldHermesReferenceArmWithoutEvent를 분리해 shadow 반사실로만 평가하고 놓친 매수로 "
    "확정하지 마라. "
)

PANEL_RESEARCH_RULES = (
    "먼저 제공 JSON으로 판단하라. 내부 paper 사실이 생략됐거나 서로 충돌할 때만 "
    "toss_paper_panel_evidence를 최대 2회 호출하라. panelId는 아래 PANEL_ID를 그대로 "
    "쓰고, 전체 장부는 session-summary, 원인 검증은 symbol-trace(종목 최대 10개)를 쓴다. "
    "임의 SQL·terminal·파일·Grafana·Toss API·주문/쓰기 도구는 금지한다. 외부 시장 사실이 "
    "꼭 필요하면 KRX·KIS Developers·OpenDART·공공데이터포털 공식 웹만 최대 3개 문서를 "
    "검색하고 URL·게시/관측 시각을 적어라. 패널 cutoff 뒤 공개된 사실은 "
    "post-cutoff-research로 표시하고 당시 매매 입력이나 놓친 매수의 증거로 쓰지 마라. "
    "검색 결과는 [검색 근거]에 tool/topic 또는 공식 URL과 cutoff 적합성을 남겨라. "
    "찾지 못한 정보는 '패널 JSON 생략', '원천 데이터 없음', '검색 안 함' 중 하나로 "
    "구분하라. missing-price-setup은 가격 자료 누락이 아니라 정상 패턴 미충족이다. "
)

IMPROVEMENT_DEBATE = (
    "정상 작동 여부 감사에 그치지 말고 오늘 evidence에서 후보 발굴·진입·사이징·"
    "청산·데이터·설명력을 개선할 반증 가능한 가설을 찾아라. 각 가설은 문제, 근거, "
    "최소 변경, shadow/fixture 검증, 성공 지표, 반증·중단 조건을 포함한다. 개선 "
    "판정에 필요한 내부 사실이 패널 JSON에서 생략됐으면 허용된 evidence 도구로 "
    "확인하라. 사후 상승만으로 gate 완화나 소급 체결을 제안하지 말고, 근거가 "
    "부족하면 규칙 변경 대신 다음 측정 항목을 제안하라. "
)


def _cursor_mcp_config() -> dict[str, Any]:
    return {"mcpServers": {"toss-panel": {"url": PAPER_MCP_URL}}}


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{AUTOMATION_URL}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        raise RuntimeError(f"automation request failed: {path}") from error
    if not isinstance(result, dict):
        raise TypeError(f"automation response is invalid: {path}")
    return result


def _cursor_call(name: str, prompt: str) -> dict[str, Any]:
    spec = ROLES[name]
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix=f"toss-panel-{name}-") as temp_dir:
        cursor_dir = Path(temp_dir) / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(
            json.dumps(_cursor_mcp_config(), separators=(",", ":"))
        )
        process = subprocess.run(
            [
                CURSOR_AGENT,
                "-p",
                "--trust",
                "--mode",
                "ask",
                "--approve-mcps",
                "--workspace",
                temp_dir,
                "--model",
                str(spec["model"]),
                "--output-format",
                "json",
                prompt,
            ],
            cwd=temp_dir,
            env={
                **os.environ,
                "HOME": "/opt/data",
                "XDG_CONFIG_HOME": "/opt/data/.config",
            },
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finished_at = datetime.now(UTC)
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        suffix = f": {detail[:1000]}" if detail else ""
        raise RuntimeError(
            f"{name} agent failed with exit {process.returncode}{suffix}"
        )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} agent returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise TypeError(f"{name} agent returned invalid JSON")
    content = payload.get("result") or payload.get("content") or payload.get("text")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{name} agent returned empty content")
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = _token(usage, "inputTokens", "promptTokens")
    completion_tokens = _token(usage, "outputTokens", "completionTokens")
    return {
        "role": str(spec["role"]),
        "provider": "cursor-agent",
        "model": str(spec["model"]),
        "content": content.strip()[:5000],
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": max(
            _token(usage, "totalTokens"), prompt_tokens + completion_tokens
        ),
        "cacheReadTokens": _token(usage, "cacheReadTokens"),
        "cacheWriteTokens": _token(usage, "cacheWriteTokens"),
    }


def _token(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _briefing(context: dict[str, Any]) -> tuple[str, str, str]:
    value = context.get("briefing")
    kind = value.get("kind") if isinstance(value, dict) else "close"
    if kind == "hourly":
        return (
            "시간별 시장 감시",
            (
                "현재 시각까지 저장된 감시 표본이다. 사후 가격으로 당시 매수 가능성을 "
                "확정하거나 실시간 전략을 바꾸지 마라."
            ),
            "[시간별 결론], [놓친 후보], [원인], [보완 실험], [데이터 상태]",
        )
    if kind == "midday":
        return (
            "장중 중간 브리핑",
            "현재까지의 관측치다. 종가·일일 수익·장 마감 결과로 확정하지 마라.",
            "[중간 결론], [합의], [이견/이상], [개선 후보], [Risk], [오후 확인]",
        )
    return (
        "장마감 브리핑",
        "마감 시점 자료이지만 제공 JSON 밖의 종가나 성과를 추정하지 마라.",
        "[오늘 결론], [합의], [이견/이상], [개선 후보], [Risk], [내일 확인]",
    )


def _independent_prompt(
    name: str, context: dict[str, Any], *, panel_id: str
) -> str:
    spec = ROLES[name]
    title, timing_guard, _ = _briefing(context)
    return (
        f"너는 Toss Trader paper {title} 패널의 "
        f"{spec['role']}다. {spec['instruction']} "
        f"{timing_guard} "
        "다른 분석가 의견은 아직 없다. "
        f"{PANEL_RESEARCH_RULES}"
        "middaySnapshotV2가 있으면 마지막 사유만 반복하지 말고 firstReason→lastReason "
        "변화, transitionCount, reasonClass, changedFacts를 우선 분석하라. "
        "changedFacts가 비었으면 '새로 바뀐 핵심 사실 없음'이라고 짧게 쓰고 같은 결론을 "
        "늘여 쓰지 마라. 정상 조건 탈락과 실제 missing-data/error를 구분하라. "
        f"{IMPROVEMENT_DEBATE}"
        "반드시 [개선 가설]에 우선순위가 가장 높은 제안 1~2개와 shadow/fixture "
        "검증, 성공 지표, 반증 조건을 적어라. 증거가 없으면 [개선 가설] 없음과 "
        "필요한 다음 측정을 적어라. "
        f"{MARKET_CRITIQUE}"
        "매매 지시·수익 보장 금지. 핵심 근거와 불확실성을 한국어 1200자 이내로 작성.\n"
        f"PANEL_ID={panel_id}\n"
        f"TODAY_JSON={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def _review_prompt(
    name: str,
    context: dict[str, Any],
    independent: dict[str, dict[str, Any]],
    *,
    panel_id: str,
) -> str:
    spec = ROLES[name]
    title, timing_guard, _ = _briefing(context)
    opinions = {key: value["content"] for key, value in independent.items()}
    return (
        f"너는 Toss Trader paper {title} 패널의 "
        f"{spec['role']}다. 세 독립 의견을 모두 검토하라. "
        f"{timing_guard} "
        "합의점, 충돌, 틀린 주장/과잉해석, 최종 judge가 남겨야 할 불확실성을 "
        "사유 변화와 changedFacts 중심으로 검토하고 새 사실이 없으면 반복을 지적하라. "
        f"{IMPROVEMENT_DEBATE}"
        "다른 분석가의 개선 가설 각각을 채택/기각/추가 자료로 판정하고, 중복 제안은 "
        "합치며 숨은 비용과 반증 조건을 지적하라. "
        "독립 의견의 [검색 근거]를 먼저 재사용하고, 아직 풀리지 않은 충돌에만 "
        f"{PANEL_RESEARCH_RULES}"
        f"{MARKET_CRITIQUE}"
        "한국어 900자 이내 작성. 매매 지시 금지.\n"
        f"PANEL_ID={panel_id}\n"
        f"TODAY_JSON={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}\n"
        f"INDEPENDENT={json.dumps(opinions, ensure_ascii=False, separators=(',', ':'))}"
    )


def _hermes_call(
    context: dict[str, Any],
    independent: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    *,
    panel_id: str,
) -> dict[str, Any]:
    return _hermes_prompt_call(
        _judge_prompt(
            context,
            independent,
            reviews,
            panel_id=panel_id,
        )
    )


def _judge_prompt(
    context: dict[str, Any],
    independent: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    *,
    panel_id: str,
) -> str:
    title, timing_guard, sections = _briefing(context)
    evidence = {
        "today": context,
        "independent": {key: value["content"] for key, value in independent.items()},
        "reviews": {key: value["content"] for key, value in reviews.items()},
    }
    prompt = (
        f"너는 Toss Trader paper {title} 패널의 최종 judge Hermes다. GPT quant, "
        "Grok skeptic, Gemini Risk의 독립 의견과 상호검토를 판정하라. 제공된 "
        f"evidence 밖의 사실을 만들지 마라. {timing_guard} "
        "각 의견의 [검색 근거]를 검증하고, 결론에 필요한 내부 충돌이 남았을 때만 "
        f"{PANEL_RESEARCH_RULES}"
        f"{IMPROVEMENT_DEBATE}"
        "[개선 후보]에는 상호검토를 통과한 최대 2개만 남겨라. 각 후보에 우선순위, "
        "문제와 evidence, 최소 변경, shadow/fixture 검증, 성공 지표와 중단 조건을 "
        "적어라. 당일 실전 규칙은 자동 변경하지 않는다. 채택할 근거가 없으면 "
        "'개선 후보 없음'과 다음 측정만 적어라. "
        f"텔레그램용 한국어 평문으로 {sections}을 포함해 2800자 이내 작성하라. "
        f"{MARKET_CRITIQUE}"
        "매매 지시·수익 보장 금지.\n"
        f"PANEL_ID={panel_id}\n"
        f"EVIDENCE={json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}"
    )
    return prompt


def _hourly_call(context: dict[str, Any], *, panel_id: str) -> dict[str, Any]:
    prompt = (
        "너는 Toss Trader paper 시간별 시장 감시의 anomaly judge Hermes다. "
        "hourlyWatchV1.priorHourlyReviewsV1의 같은 날 이전 결론을 먼저 읽고 현재 "
        "evidence와 대조하라. 이미 설명한 원인·보완 실험·데이터 상태는 상태가 "
        "달라지지 않았다면 다시 서술하지 마라. 새 사실이나 바뀐 수치가 없으면 "
        "'[시간별 결론] 새로 바뀐 핵심 사실 없음 — 이전 결론 유지.' 한 줄로 끝내라. "
        "hourlyWatchV1.anomalies를 우선 검증하고 시장 급변, 데이터·운영 장애, "
        "신호가 없었던 뒤 강하게 오른 감시종목의 거절 원인을 구분하라. "
        "hindsight-review-candidate는 사후 검토 후보일 뿐 놓친 체결 가능 매수나 "
        "수익 증거가 아니다. 게이트 완화·소급 체결·즉시 매매를 제안하지 말고, "
        "원인이 데이터/실행/Risk/전략 중 무엇인지와 다음 shadow·fixture 검증만 적어라. "
        "내부 사실이 생략됐거나 충돌할 때만 toss_paper_panel_evidence를 최대 2회, "
        "시장 사건 확인이 결론에 꼭 필요할 때만 KRX·KIS Developers·OpenDART·"
        "공공데이터포털 공식 웹을 최대 3개 검색하라. URL과 게시/관측 시각을 적고, "
        "cutoff 뒤 공개 사실은 post-cutoff-research로 표시해 당시 매매 입력으로 "
        "쓰지 마라. missing-price-setup은 데이터 누락이 아닌 정상 가격패턴 탈락이다. "
        "새 내용이 있을 때만 [시간별 결론], [새로 바뀐 점], [놓친 후보], [원인], "
        "[보완 실험], [데이터 상태] 순서로 "
        "텔레그램용 한국어 2400자 이내. 매매 지시·수익 보장 금지.\n"
        f"PANEL_ID={panel_id}\n"
        f"EVIDENCE={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )
    opinion = _hermes_prompt_call(prompt, content_limit=3200)
    opinion["role"] = "hourly anomaly judge"
    return opinion


def _hermes_prompt_call(
    prompt: str, *, content_limit: int = 3800
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="toss-panel-") as temp_dir:
        usage_path = Path(temp_dir) / "usage.json"
        process = subprocess.run(
            [
                HERMES,
                "--ignore-rules",
                "--toolsets",
                "web,toss-panel",
                "--provider",
                "openai-codex",
                "-m",
                "gpt-5.6-terra",
                "--usage-file",
                str(usage_path),
                "-z",
                prompt,
            ],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
    finished_at = datetime.now(UTC)
    completed = usage.get("completed") is True and usage.get("failed") is not True
    if not process.stdout.strip() or (process.returncode != 0 and not completed):
        raise RuntimeError(f"Hermes judge failed with exit {process.returncode}")
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = _token(usage, "prompt_tokens", "input_tokens", "inputTokens")
    completion_tokens = _token(
        usage, "completion_tokens", "output_tokens", "outputTokens"
    )
    return {
        "stage": "judge:hermes",
        "role": "final judge",
        "provider": str(usage.get("provider") or "hermes"),
        "model": str(usage.get("model") or "hermes-default"),
        "content": process.stdout.strip()[:content_limit],
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": max(
            _token(usage, "total_tokens", "totalTokens"),
            prompt_tokens + completion_tokens,
        ),
        "cacheReadTokens": _token(usage, "cache_read_tokens", "cacheReadTokens"),
        "cacheWriteTokens": _token(
            usage, "cache_write_tokens", "cacheWriteTokens"
        ),
    }


def _run_round(
    prompts: dict[str, str], *, panel_id: str, stage_prefix: str
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    errors: list[Exception] = []
    for name, prompt in prompts.items():
        try:
            opinion = _cursor_call(name, prompt)
        except Exception as error:  # noqa: BLE001 - preserve peer results
            errors.append(error)
            continue
        opinion["stage"] = f"{stage_prefix}:{name}"
        results[name] = opinion
        _post(
            "/workflow/daily-panel-opinion",
            {"panelId": panel_id, "opinion": opinion},
        )
    if errors:
        raise errors[0]
    return results


def main() -> int:
    claimed = _post("/workflow/daily-panel-claim", {})
    if not claimed.get("claimed"):
        return 0
    panel_id = str(claimed["panelId"])
    context = claimed.get("context")
    if not isinstance(context, dict):
        raise TypeError("claimed panel context is invalid")
    briefing = context.get("briefing")
    briefing = briefing if isinstance(briefing, dict) else {}
    hourly = briefing.get("kind") == "hourly"
    opinions: list[dict[str, Any]] = []
    try:
        if hourly:
            judge = _hourly_call(context, panel_id=panel_id)
            _post(
                "/workflow/hourly-panel-complete",
                {"panelId": panel_id, "opinion": judge},
            )
            return 0
        independent = _run_round(
            {
                name: _independent_prompt(name, context, panel_id=panel_id)
                for name in ROLES
            },
            panel_id=panel_id,
            stage_prefix="independent",
        )
        opinions.extend(independent.values())

        reviews = _run_round(
            {
                name: _review_prompt(
                    name, context, independent, panel_id=panel_id
                )
                for name in ROLES
            },
            panel_id=panel_id,
            stage_prefix="review",
        )
        opinions.extend(reviews.values())

        judge = _hermes_call(context, independent, reviews, panel_id=panel_id)
        opinions.append(judge)
        _post(
            "/workflow/daily-panel-opinion",
            {"panelId": panel_id, "opinion": judge},
        )
        _post(
            "/workflow/daily-panel-complete",
            {"panelId": panel_id, "opinions": opinions},
        )
    except Exception as error:
        _post(
            (
                "/workflow/hourly-panel-fail"
                if hourly
                else "/workflow/daily-panel-fail"
            ),
            {"panelId": panel_id, "error": str(error)[:1000]},
        )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
