"""Tests for Langfuse OTLP tracing (agent_eval.core.tracing) and its call sites.

Uses the OpenTelemetry SDK InMemorySpanExporter; no real Langfuse needed.
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent_eval.agents.inference import InferenceConfig
from agent_eval.core import tracing
from agent_eval.core.artifact_store import create_artifact_store
from agent_eval.core.settings import Settings
from agent_eval.core.tracing import (
    endpoint_host,
    init_tracing,
    set_attributes,
    shutdown_tracing,
    trace_span,
    tracing_enabled,
)
from agent_eval.db.enums import Ability, AuthType, QuestionType
from agent_eval.worker import limits as worker_limits
from agent_eval.worker.executor import (
    AgentCircuitBreaker,
    AgentWork,
    WorkItem,
    agent_worker,
)
from agent_eval.worker.scoring import JudgeRuntime, ScoreWork, judge_worker
from agent_eval.workspace import executor as workspace_executor
from agent_eval.workspace.queue import WorkspaceClaim


@pytest.fixture(autouse=True)
def reset_tracing_state() -> Iterator[None]:
    tracing._reset_for_tests()
    yield
    tracing._reset_for_tests()


def langfuse_settings() -> Settings:
    return Settings(
        LANGFUSE_HOST="http://langfuse.test",
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
    )


def agent_config(endpoint_url: str = "http://agent.test/run") -> InferenceConfig:
    return InferenceConfig(
        endpoint_url=endpoint_url,
        auth_type=AuthType.NONE,
        token=None,
        request_template='{"query":"{{prompt}}"}',
        answer_jsonpath="$.answer",
        citations_jsonpath=None,
        timeout_seconds=10,
    )


async def no_sleep(_: float) -> None:
    return None


def test_tracing_disabled_without_configuration() -> None:
    assert init_tracing(Settings()) is False
    assert tracing_enabled() is False
    with trace_span("noop", attributes={"a": "b"}) as span:
        assert span is None
    shutdown_tracing()


def test_tracing_disabled_with_partial_configuration() -> None:
    settings = Settings(LANGFUSE_HOST="http://langfuse.test")
    assert settings.langfuse_enabled is False
    assert init_tracing(settings) is False
    assert tracing_enabled() is False


def test_span_hierarchy_and_attributes() -> None:
    exporter = InMemorySpanExporter()
    assert init_tracing(langfuse_settings(), exporter=exporter) is True
    with trace_span("root", attributes={"agent_eval.run_id": "r1", "dropped": None}) as root:
        with trace_span("child", attributes={"gen_ai.usage.input_tokens": 5}):
            pass
        set_attributes(root, {"agent_eval.success": True})
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"root", "child"}
    assert spans["child"].parent.span_id == spans["root"].context.span_id
    assert spans["child"].context.trace_id == spans["root"].context.trace_id
    assert dict(spans["root"].attributes) == {
        "agent_eval.run_id": "r1",
        "agent_eval.success": True,
    }
    assert dict(spans["child"].attributes) == {"gen_ai.usage.input_tokens": 5}
    shutdown_tracing()
    assert tracing_enabled() is False


class FailingExporter:
    def export(self, spans: object) -> object:
        raise RuntimeError("otlp boom")

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def test_exporter_failure_never_breaks_execution() -> None:
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = ListHandler()
    tracing_logger = logging.getLogger("agent_eval.core.tracing")
    # alembic's fileConfig (disable_existing_loggers) may have disabled the
    # module logger in an earlier migration-running test; re-enable it here.
    tracing_logger.disabled = False
    tracing_logger.addHandler(handler)
    try:
        init_tracing(langfuse_settings(), exporter=FailingExporter())
        with trace_span("first"):
            pass
        with trace_span("second"):
            pass
    finally:
        tracing_logger.removeHandler(handler)
    warnings = [
        record for record in records if record.getMessage() == "tracing_exporter_failed"
    ]
    assert len(warnings) == 1
    shutdown_tracing()


def test_span_records_exceptions() -> None:
    exporter = InMemorySpanExporter()
    init_tracing(langfuse_settings(), exporter=exporter)
    with pytest.raises(ValueError, match="nope"), trace_span("failing"):
        raise ValueError("nope")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_endpoint_host() -> None:
    assert endpoint_host("https://api.example.com/v1/chat") == "api.example.com"
    assert endpoint_host("not-a-url") == "unknown"


def success_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "answer": "42",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    )


async def run_agent_worker(
    handler: httpx.AsyncBaseTransport,
) -> tuple[list[object], AgentWork, WorkItem]:
    finalized: list[object] = []

    class FakeWriter:
        async def start_attempt(self, item_id: object, attempt_no: int) -> object:
            return uuid4()

        async def finish_attempt(self, completion: object) -> None:
            return None

        async def finalize_item(self, **kwargs: object) -> None:
            finalized.append(kwargs)

    item = WorkItem(
        uuid4(), uuid4(), uuid4(), 0, "question?", QuestionType.SHORT_ANSWER, "42", 0
    )
    work = AgentWork(
        run_id=uuid4(),
        run_agent_id=item.run_agent_id,
        agent_id=uuid4(),
        version_id=uuid4(),
        config=agent_config(),
        concurrency=1,
        items=[item],
    )
    queue: asyncio.Queue[WorkItem] = asyncio.Queue()
    queue.put_nowait(item)
    async with httpx.AsyncClient(transport=handler) as client:
        await agent_worker(queue, work, FakeWriter(), client, no_sleep, AgentCircuitBreaker())
    return finalized, work, item


async def test_agent_worker_emits_item_and_agent_spans() -> None:
    exporter = InMemorySpanExporter()
    init_tracing(langfuse_settings(), exporter=exporter)
    finalized, work, item = await run_agent_worker(httpx.MockTransport(success_handler))
    assert len(finalized) == 1

    spans = exporter.get_finished_spans()
    (item_span,) = [span for span in spans if span.name.startswith("eval.item run=")]
    (agent_span,) = [span for span in spans if span.name == "gen_ai.chat agent"]
    assert str(work.run_id) in item_span.name
    assert str(item.question_id) in item_span.name
    assert agent_span.parent.span_id == item_span.context.span_id
    assert agent_span.context.trace_id == item_span.context.trace_id
    agent_attributes = dict(agent_span.attributes)
    assert agent_attributes["gen_ai.system"] == "agent.test"
    assert agent_attributes["gen_ai.usage.input_tokens"] == 5
    assert agent_attributes["gen_ai.usage.output_tokens"] == 2
    assert agent_attributes["gen_ai.usage.total_tokens"] == 7
    assert "error.type" not in agent_attributes
    item_attributes = dict(item_span.attributes)
    assert item_attributes["agent_eval.success"] is True
    assert item_attributes["agent_eval.question_type"] == QuestionType.SHORT_ANSWER.value


async def test_agent_worker_records_error_type() -> None:
    exporter = InMemorySpanExporter()
    init_tracing(langfuse_settings(), exporter=exporter)

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    finalized, _, _ = await run_agent_worker(httpx.MockTransport(failing_handler))
    assert len(finalized) == 1

    spans = exporter.get_finished_spans()
    (agent_span,) = [span for span in spans if span.name == "gen_ai.chat agent"]
    (item_span,) = [span for span in spans if span.name.startswith("eval.item run=")]
    assert dict(agent_span.attributes)["error.type"] == "agent_server_error"
    item_attributes = dict(item_span.attributes)
    assert item_attributes["error.type"] == "agent_server_error"
    assert item_attributes["agent_eval.success"] is False


async def test_judge_worker_span_records_score_model_and_usage() -> None:
    exporter = InMemorySpanExporter()
    init_tracing(langfuse_settings(), exporter=exporter)
    run_id = uuid4()
    item_id = uuid4()

    def judge_handler(request: httpx.Request) -> httpx.Response:
        verdict = {"verdict": "correct", "reason": "Equivalent"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(verdict)}}],
                "model": "deepseek-v4-flash-20260901",
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            },
        )

    class FakeScoringWriter:
        async def write(self, result: object) -> bool:
            return True

    queue: asyncio.Queue[ScoreWork] = asyncio.Queue()
    queue.put_nowait(
        ScoreWork(
            item_id=item_id,
            ability=Ability.REASONING,
            question_type=QuestionType.SHORT_ANSWER,
            question="question",
            reference_answer="42",
            candidate_answer="42",
            valid_labels=[],
            requires_citations=False,
            failed=False,
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(judge_handler)) as client:
        completed = await judge_worker(
            queue,
            FakeScoringWriter(),
            client,
            Settings(deepseek_api_key="sk-test"),
            JudgeRuntime(
                provider="deepseek",
                model="deepseek-v4-flash",
                prompt_version="short-v1",
            ),
            no_sleep,
            run_id=run_id,
        )
    assert completed is True

    (span,) = exporter.get_finished_spans()
    assert span.name == f"eval.judge run={run_id} item={item_id}"
    attributes = dict(span.attributes)
    assert attributes["gen_ai.system"] == "deepseek"
    assert attributes["gen_ai.operation.name"] == "judge"
    assert attributes["gen_ai.request.model"] == "deepseek-v4-flash"
    assert attributes["gen_ai.response.model"] == "deepseek-v4-flash-20260901"
    assert attributes["agent_eval.judge_score"] == 100
    assert attributes["agent_eval.judge_attempts"] == 1
    assert attributes["gen_ai.usage.input_tokens"] == 11
    assert attributes["gen_ai.usage.output_tokens"] == 3
    assert attributes["gen_ai.usage.total_tokens"] == 14


async def test_workspace_attempt_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = InMemorySpanExporter()
    init_tracing(langfuse_settings(), exporter=exporter)
    settings = Settings(data_dir=tmp_path)
    claim = WorkspaceClaim(attempt_id=uuid4(), claim_token="token")
    work = workspace_executor.WorkspaceWork(
        attempt_id=claim.attempt_id,
        conversation_id=uuid4(),
        user_message_id=uuid4(),
        agent_version_id=uuid4(),
        generation_no=1,
        concurrency=1,
        prompt="hello",
        config=agent_config(),
    )

    async def fake_load(
        session_factory: object, claim_arg: object, owner_id: object, token_cipher: object
    ) -> workspace_executor.WorkspaceWork:
        return work

    finalized: list[object] = []

    async def fake_finalize(
        session_factory: object,
        settings_arg: object,
        claim_arg: object,
        owner_id: object,
        outcome: object,
        stored: object,
        **kwargs: object,
    ) -> bool:
        finalized.append(outcome)
        return True

    async def fake_watch(*args: object) -> None:
        await asyncio.Event().wait()

    async def fake_start_attempt(
        session_factory: object, claim_arg: object, owner_id: object, attempt_no: int
    ) -> object:
        return uuid4()

    async def fake_finish_attempt(
        session_factory: object, claim_arg: object, owner_id: object, completion: object
    ) -> None:
        return None

    monkeypatch.setattr(workspace_executor, "load_workspace_work", fake_load)
    monkeypatch.setattr(workspace_executor, "finalize_workspace_attempt", fake_finalize)
    monkeypatch.setattr(workspace_executor, "watch_cancellation", fake_watch)
    monkeypatch.setattr(workspace_executor, "start_http_attempt", fake_start_attempt)
    monkeypatch.setattr(workspace_executor, "finish_http_attempt", fake_finish_attempt)

    result = await workspace_executor.process_workspace_attempt(
        None,  # session_factory: unused with the collaborators above mocked
        settings,
        claim,
        artifact_store=create_artifact_store(settings),
        token_cipher=None,
        owner_id="worker",
        limits=worker_limits.WorkerLimits.from_settings(settings),
        transport=httpx.MockTransport(success_handler),
    )
    assert result is True
    assert len(finalized) == 1 and finalized[0].success

    spans = exporter.get_finished_spans()
    (root,) = [span for span in spans if span.name == f"workspace.attempt {claim.attempt_id}"]
    (agent_span,) = [span for span in spans if span.name == "gen_ai.chat agent"]
    assert agent_span.parent.span_id == root.context.span_id
    agent_attributes = dict(agent_span.attributes)
    assert agent_attributes["gen_ai.usage.input_tokens"] == 5
    assert agent_attributes["agent_eval.streaming"] is False
    assert agent_attributes["agent_eval.success"] is True
    root_attributes = dict(root.attributes)
    assert root_attributes["agent_eval.conversation_id"] == str(work.conversation_id)
    assert root_attributes["agent_eval.agent_version_id"] == str(work.agent_version_id)
    assert root_attributes["agent_eval.success"] is True
