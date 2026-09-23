import json
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
import pytest

from agent_eval.agents.client import ConnectionConfig
from agent_eval.agents.inference import (
    MAX_RESPONSE_BYTES,
    AttemptCompletion,
    InferenceConfig,
    execute_inference,
    post_agent_prompt,
)
from agent_eval.agents.preflight import _build_probes
from agent_eval.agents.prompt import PromptBuildError, build_question_prompt
from agent_eval.db.enums import Ability, AttemptStatus, AuthType, Language, QuestionType
from agent_eval.db.models import Benchmark, DatasetVersion, Question


class AttemptRecorder:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.completed: list[AttemptCompletion] = []

    async def start(self, attempt_no: int) -> UUID:
        self.started.append(attempt_no)
        return uuid4()

    async def finish(self, completion: AttemptCompletion) -> None:
        self.completed.append(completion)


def config(*, token: str | None = None) -> InferenceConfig:
    return InferenceConfig(
        endpoint_url="https://agent.example/run",
        auth_type=AuthType.BEARER if token else AuthType.NONE,
        token=token,
        request_template='{"query":"{{prompt}}"}',
        answer_jsonpath="$.answer",
        citations_jsonpath="$.citations",
        timeout_seconds=5,
    )


async def no_sleep(_: float) -> None:
    return None


def test_prompt_builder_formats_each_question_type() -> None:
    multiple_choice = build_question_prompt(
        question_type=QuestionType.MULTIPLE_CHOICE,
        prompt="Which one?",
        choices=[{"label": "A", "text": "First"}, {"label": "B", "text": "Second"}],
        requires_citations=False,
    )
    short_answer = build_question_prompt(
        question_type=QuestionType.SHORT_ANSWER,
        prompt="Find it",
        choices=None,
        requires_citations=True,
    )
    open_answer = build_question_prompt(
        question_type=QuestionType.OPEN_ENDED,
        prompt="Explain it",
        choices=None,
        requires_citations=False,
    )
    exact_instruction = build_question_prompt(
        question_type=QuestionType.SHORT_ANSWER,
        prompt="Output only A|B",
        choices=None,
        requires_citations=False,
        ability=Ability.INSTRUCTION_FOLLOWING,
    )

    assert "A. First" in multiple_choice
    assert "只返回一个选项标识" in multiple_choice
    assert "可访问来源链接" in short_answer
    assert "不要输出内部分析过程" in open_answer
    assert exact_instruction == "Output only A|B"
    with pytest.raises(PromptBuildError, match="no choices"):
        build_question_prompt(
            question_type=QuestionType.MULTIPLE_CHOICE,
            prompt="Invalid",
            choices=None,
            requires_citations=False,
        )


def test_long_context_preflight_uses_a_real_full_length_prompt() -> None:
    benchmark = Benchmark(
        name="Long Context",
        ability=Ability.LONG_CONTEXT,
        language=Language.EN,
        source="internal",
    )
    dataset = DatasetVersion(
        benchmark=benchmark,
        version=1,
        source_version="v1",
        content_hash="a" * 64,
        question_count=1,
    )
    question = Question(
        dataset_version=dataset,
        external_id="long-1",
        type=QuestionType.SHORT_ANSWER,
        prompt="x" * 8_500,
        reference_answer="answer",
    )
    probes = _build_probes(
        ConnectionConfig(
            endpoint_url="https://agent.example/run",
            auth_type=AuthType.NONE,
            token=None,
            request_template='{"query":"{{prompt}}"}',
            answer_jsonpath="$.answer",
            citations_jsonpath=None,
            timeout_seconds=5,
        ),
        [question],
        max_prompt_characters=9_000,
    )

    representative = next(probe for probe in probes if probe.key == "representative")
    assert len(representative.prompt) > 8_000
    assert representative.prompt.startswith("x" * 100)


async def test_inference_success_redacts_token_and_limits_citations() -> None:
    recorder = AttemptRecorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert json.loads(request.content) == {"query": "question"}
        return httpx.Response(
            200,
            json={
                "answer": "answer",
                "citations": [f"https://example.com/{index}" for index in range(4)],
                "debug": "secret-token",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await execute_inference(
            client,
            config(token="secret-token"),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
            sleep=no_sleep,
        )

    assert outcome.success is True
    assert outcome.answer == "answer"
    assert len(outcome.citations) == 3
    assert outcome.response_content is not None
    assert b"secret-token" not in outcome.response_content
    assert b"[REDACTED]" in outcome.response_content
    assert recorder.started == [1]
    assert [item.status for item in recorder.completed] == [AttemptStatus.SUCCEEDED]


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            (12, 5, 17),
        ),
        ({"input_tokens": 9, "output_tokens": 4}, (9, 4, 13)),
        (
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 99},
            (7, 3, None),
        ),
        (
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": "10"},
            (7, 3, None),
        ),
        (
            {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "2"},
            (None, None, None),
        ),
        (
            {"prompt_tokens": 7, "input_tokens": 8, "output_tokens": 2},
            (None, 2, None),
        ),
        (None, (None, None, None)),
    ],
)
async def test_inference_safely_extracts_standard_token_usage(
    usage: object, expected: tuple[int | None, int | None, int | None]
) -> None:
    recorder = AttemptRecorder()

    async def handler(_: httpx.Request) -> httpx.Response:
        payload = {"answer": "answer", "citations": []}
        if usage is not None:
            payload["usage"] = usage
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
        )

    assert outcome.success is True
    completion = recorder.completed[0]
    assert (completion.input_tokens, completion.output_tokens, completion.total_tokens) == expected


async def test_inference_redacts_json_escaped_token_everywhere() -> None:
    recorder = AttemptRecorder()
    token = 'secret-"-\\-escaped'

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": f"answer {token}",
                "citations": [f"https://example.com/{token}"],
                token: [token],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await execute_inference(
            client,
            config(token=token),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
        )

    assert outcome.response_content is not None
    assert token not in outcome.response_content.decode()
    assert token not in (outcome.answer or "")
    assert all(token not in citation for citation in outcome.citations)
    assert outcome.response_content.count(b"[REDACTED]") == 4


async def test_429_retries_once_then_succeeds() -> None:
    recorder = AttemptRecorder()
    calls = 0
    delays: list[float] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "limit"})
        return httpx.Response(200, json={"answer": "ok", "citations": []})

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
            sleep=record_sleep,
        )

    assert outcome.success is True
    assert outcome.attempt_count == 2
    assert recorder.started == [1, 2]
    assert [item.error_type for item in recorder.completed] == ["agent_rate_limit", None]
    assert delays == [0]


@pytest.mark.parametrize(
    ("handler", "expected_error", "expected_attempts"),
    [
        (
            lambda _: httpx.Response(503, json={"error": "unavailable"}),
            "agent_server_error",
            2,
        ),
        (lambda _: httpx.Response(400, json={"error": "bad"}), "agent_client_error", 1),
        (
            lambda _: httpx.Response(
                400, json={"error": "Range of input length should be [1, 3072]"}
            ),
            "agent_input_limit",
            1,
        ),
        (lambda _: httpx.Response(401, json={"error": "bad key"}), "agent_authentication", 1),
        (lambda _: httpx.Response(200, content=b"not-json"), "agent_response_schema", 1),
        (
            lambda _: httpx.Response(200, json={"answer": "", "citations": []}),
            "agent_response_schema",
            1,
        ),
    ],
)
async def test_inference_failure_retry_policy(
    handler: Callable[[httpx.Request], httpx.Response],
    expected_error: str,
    expected_attempts: int,
) -> None:
    recorder = AttemptRecorder()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
            sleep=no_sleep,
        )

    assert outcome.success is False
    assert outcome.error_type == expected_error
    assert outcome.attempt_count == expected_attempts
    assert len(recorder.completed) == expected_attempts


async def test_timeout_retries_once() -> None:
    recorder = AttemptRecorder()

    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
            sleep=no_sleep,
        )

    assert outcome.success is False
    assert outcome.error_type == "agent_timeout"
    assert recorder.started == [1, 2]


async def test_network_error_retries_once() -> None:
    recorder = AttemptRecorder()

    async def network_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(network_error)) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
            sleep=no_sleep,
        )

    assert outcome.success is False
    assert outcome.error_type == "agent_network"
    assert recorder.started == [1, 2]


async def test_oversized_response_is_not_retained() -> None:
    recorder = AttemptRecorder()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 100))
    )
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=recorder.start,
            finish_attempt=recorder.finish,
            sleep=no_sleep,
        )

    assert outcome.success is False
    assert outcome.error_type == "agent_response_too_large"
    assert outcome.response_content is None


async def test_cancellation_before_attempt_prevents_http_request() -> None:
    requests = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"answer": "unexpected"})

    async def canceled_start(_: int) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await execute_inference(
            client,
            config(),
            "question",
            previous_attempt_count=0,
            start_attempt=canceled_start,
            finish_attempt=AttemptRecorder().finish,
            sleep=no_sleep,
        )

    assert outcome.canceled is True
    assert requests == 0

def post_config(endpoint_url: str) -> InferenceConfig:
    return InferenceConfig(
        endpoint_url=endpoint_url,
        auth_type=AuthType.NONE,
        token=None,
        request_template='{"query":"{{prompt}}"}',
        answer_jsonpath="$.answer",
        citations_jsonpath=None,
        timeout_seconds=5,
    )


def test_post_agent_prompt_extracts_openai_style_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "ok",
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = post_agent_prompt(client, post_config("http://agent.test/run"), "hello")
    assert result.answer == "ok"
    assert (result.input_tokens, result.output_tokens) == (120, 30)
    assert result.total_tokens == 150  # computed from input+output when absent


def test_post_agent_prompt_without_usage_reports_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = post_agent_prompt(client, post_config("http://agent.test/run"), "hello")
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.total_tokens is None

