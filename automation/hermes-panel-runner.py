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

ROLES = {
    "gpt": {
        "role": "quant analyst",
        "model": "gpt-5.6-sol-medium",
        "instruction": (
            "수익률·체결·신호·후보 funnel을 정량 분석하라. 비교 가능한 수치와 "
            "데이터 한계를 구분하고, 제공되지 않은 값을 추정하지 마라."
        ),
    },
    "grok": {
        "role": "skeptic / anomaly detector",
        "model": "cursor-grok-4.6-high-fast",
        "instruction": (
            "이상치·장부 불일치·PIT/미래참조·데이터 누락·운영 실패 가능성을 "
            "공격적으로 찾되, JSON에 없는 사실은 만들지 마라."
        ),
    },
    "gemini": {
        "role": "Risk Manager",
        "model": "gemini-3.7-flash-high",
        "instruction": (
            "노출·현금·손실·거절 사유·체결 및 시스템 위험을 평가하라. "
            "universe membership과 주문 실행 Risk를 혼동하지 마라."
        ),
    },
}


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
    process = subprocess.run(
        [
            CURSOR_AGENT,
            "-p",
            "--trust",
            "--mode",
            "ask",
            "--model",
            str(spec["model"]),
            "--output-format",
            "json",
            prompt,
        ],
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    finished_at = datetime.now(UTC)
    if process.returncode != 0:
        raise RuntimeError(f"{name} agent failed with exit {process.returncode}")
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


def _independent_prompt(name: str, context: dict[str, Any]) -> str:
    spec = ROLES[name]
    return (
        "너는 Toss Trader paper 마감 패널의 "
        f"{spec['role']}다. {spec['instruction']} "
        "제공 JSON만 사용하고 도구를 호출하지 마라. 다른 분석가 의견은 아직 없다. "
        "매매 지시·수익 보장 금지. 핵심 근거와 불확실성을 한국어 1200자 이내로 작성.\n"
        f"TODAY_JSON={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def _review_prompt(
    name: str, context: dict[str, Any], independent: dict[str, dict[str, Any]]
) -> str:
    spec = ROLES[name]
    opinions = {key: value["content"] for key, value in independent.items()}
    return (
        "너는 Toss Trader paper 마감 패널의 "
        f"{spec['role']}다. 세 독립 의견을 모두 검토하라. "
        "합의점, 충돌, 틀린 주장/과잉해석, 최종 judge가 남겨야 할 불확실성을 "
        "제공 JSON만으로 한국어 900자 이내 작성. 매매 지시 금지.\n"
        f"TODAY_JSON={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}\n"
        f"INDEPENDENT={json.dumps(opinions, ensure_ascii=False, separators=(',', ':'))}"
    )


def _hermes_call(
    context: dict[str, Any],
    independent: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evidence = {
        "today": context,
        "independent": {key: value["content"] for key, value in independent.items()},
        "reviews": {key: value["content"] for key, value in reviews.items()},
    }
    prompt = (
        "너는 Toss Trader paper 마감 패널의 최종 judge Hermes다. GPT quant, "
        "Grok skeptic, Gemini Risk의 독립 의견과 상호검토를 판정하라. 제공된 "
        "evidence 밖의 사실을 만들지 마라. 텔레그램용 한국어 평문으로 [오늘 결론], "
        "[합의], [이견/이상], [Risk], [내일 확인]을 포함해 2800자 이내 작성하라. "
        "매매 지시·수익 보장 금지.\n"
        f"EVIDENCE={json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}"
    )
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="toss-panel-") as temp_dir:
        usage_path = Path(temp_dir) / "usage.json"
        process = subprocess.run(
            [
                HERMES,
                "--safe-mode",
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
        "content": process.stdout.strip()[:3800],
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
    opinions: list[dict[str, Any]] = []
    try:
        independent = _run_round(
            {name: _independent_prompt(name, context) for name in ROLES},
            panel_id=panel_id,
            stage_prefix="independent",
        )
        opinions.extend(independent.values())

        reviews = _run_round(
            {
                name: _review_prompt(name, context, independent)
                for name in ROLES
            },
            panel_id=panel_id,
            stage_prefix="review",
        )
        opinions.extend(reviews.values())

        judge = _hermes_call(context, independent, reviews)
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
            "/workflow/daily-panel-fail",
            {"panelId": panel_id, "error": str(error)[:1000]},
        )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
