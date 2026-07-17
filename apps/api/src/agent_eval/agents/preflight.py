from dataclasses import dataclass
from typing import Literal

import httpx

from agent_eval.agents.client import ConnectionConfig, ConnectionTestResult, test_connection
from agent_eval.agents.errors import classify_agent_failure
from agent_eval.agents.prompt import PromptBuildError, build_question_prompt
from agent_eval.db.enums import Ability
from agent_eval.db.models import Question
from agent_eval.worker.limits import RequestLimit, concurrency_slot

MAX_REPRESENTATIVE_PROMPT_CHARACTERS = 8_000


@dataclass(frozen=True)
class PreflightProbe:
    key: str
    label: str
    prompt: str
    require_citations: bool = False


@dataclass(frozen=True)
class PreflightCheckResult:
    key: str
    label: str
    status: Literal["passed", "failed", "skipped"]
    prompt_characters: int
    status_code: int | None = None
    latency_ms: int | None = None
    error_category: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "prompt_characters": self.prompt_characters,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "error_category": self.error_category,
            "error": self.error,
        }


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    checks: tuple[PreflightCheckResult, ...]


async def run_agent_preflight(
    config: ConnectionConfig,
    questions: list[Question],
    *,
    max_prompt_characters: int | None,
    transport: httpx.AsyncBaseTransport | None = None,
    request_limit: RequestLimit | None = None,
) -> PreflightResult:
    probes = _build_probes(config, questions, max_prompt_characters)
    checks: list[PreflightCheckResult] = []
    for probe in probes:
        if not probe.prompt:
            checks.append(
                PreflightCheckResult(
                    key=probe.key,
                    label=probe.label,
                    status="skipped",
                    prompt_characters=0,
                    error=probe.label,
                )
            )
            continue
        async with concurrency_slot(request_limit):
            result = await test_connection(config, probe.prompt, transport=transport)
        checks.append(_check_result(probe, result))
    return PreflightResult(
        passed=all(check.status != "failed" for check in checks),
        checks=tuple(checks),
    )


def _build_probes(
    config: ConnectionConfig,
    questions: list[Question],
    max_prompt_characters: int | None,
) -> list[PreflightProbe]:
    probes = [PreflightProbe("basic", "基础连接与答案提取", "请只回答：OK")]
    rendered = _render_questions(questions)
    long_context = [
        item
        for item in rendered
        if item[0].dataset_version.benchmark.ability == Ability.LONG_CONTEXT
        and len(item[1]) > MAX_REPRESENTATIVE_PROMPT_CHARACTERS
        and (max_prompt_characters is None or len(item[1]) <= max_prompt_characters)
    ]
    allowed_limit = min(
        max_prompt_characters or MAX_REPRESENTATIVE_PROMPT_CHARACTERS,
        MAX_REPRESENTATIVE_PROMPT_CHARACTERS,
    )
    representative = (
        max(long_context, key=lambda item: len(item[1]))
        if long_context
        else max(
            (item for item in rendered if len(item[1]) <= allowed_limit),
            key=lambda item: len(item[1]),
            default=None,
        )
    )
    if representative is not None:
        probes.append(PreflightProbe("representative", "代表性长题响应", representative[1]))
    else:
        probes.append(PreflightProbe("representative", "没有符合输入限制的代表题", ""))

    if config.citations_jsonpath is None:
        probes.append(PreflightProbe("citation", "未配置引用提取，跳过引用检查", ""))
    else:
        citation = min(
            (
                item
                for item in rendered
                if item[0].requires_citations and len(item[1]) <= allowed_limit
            ),
            key=lambda item: len(item[1]),
            default=None,
        )
        if citation is None:
            probes.append(PreflightProbe("citation", "没有符合限制的引用题", ""))
        else:
            probes.append(
                PreflightProbe(
                    "citation", "引用提取", citation[1], require_citations=True
                )
            )
    return probes


def _render_questions(questions: list[Question]) -> list[tuple[Question, str]]:
    rendered: list[tuple[Question, str]] = []
    for question in questions:
        try:
            prompt = build_question_prompt(
                question_type=question.type,
                prompt=question.prompt,
                choices=question.choices_json,
                requires_citations=question.requires_citations,
                ability=question.dataset_version.benchmark.ability,
            )
        except PromptBuildError:
            continue
        rendered.append((question, prompt))
    return rendered


def _check_result(
    probe: PreflightProbe, result: ConnectionTestResult
) -> PreflightCheckResult:
    citation_missing = probe.require_citations and not result.citations
    passed = result.success and not citation_missing
    error = "未提取到引用 URL" if citation_missing else result.error
    return PreflightCheckResult(
        key=probe.key,
        label=probe.label,
        status="passed" if passed else "failed",
        prompt_characters=len(probe.prompt),
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error_category=(
            None
            if passed
            else "citation_missing"
            if citation_missing
            else classify_agent_failure(
                status_code=result.status_code,
                error=result.error,
                content=result.raw_response,
            )
        ),
        error=error,
    )
