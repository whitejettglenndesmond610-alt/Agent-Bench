import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_eval.agents.inference import (
    AttemptCompletion,
    InferenceConfig,
    execute_streaming_inference,
)
from agent_eval.api.main import create_app
from agent_eval.auth.encryption import generate_encryption_key
from agent_eval.core.settings import Settings
from agent_eval.db.base import utc_now
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import (
    AgentVersionLifecycle,
    AuthType,
    SessionRole,
    WorkspaceAttemptStatus,
    WorkspaceMessageRole,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    WorkspaceAttempt,
    WorkspaceConversation,
    WorkspaceMessage,
)
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance.workspace_users import create_workspace_user
from agent_eval.worker.limits import WorkerLimits
from agent_eval.workspace.executor import PartialAnswerWriter, process_workspace_attempt
from agent_eval.workspace.queue import WorkspaceClaim

password_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
ENCRYPTION_KEY = generate_encryption_key()
OWNER_PASSWORD = "owner-secret"
API_KEY = "sk-stream-test-key"


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


@asynccontextmanager
async def streaming_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, FastAPI, Settings, UUID, UUID]]:
    database_path = tmp_path / "streaming.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        data_dir=tmp_path,
        environment="test",
        read_only_password_hash=password_hasher.hash("shared-secret"),
        operator_password_hash=password_hasher.hash("shared-operator-secret"),
        encryption_key=ENCRYPTION_KEY,
        workspace_stream_poll_seconds=0.05,
    )
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            owner = await create_workspace_user(
                session,
                username="stream.owner",
                display_name="Stream Owner",
                role=SessionRole.OPERATOR,
                password=OWNER_PASSWORD,
            )
            agent = Agent(name="Public Agent", provider="Public Provider")
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                endpoint_url="https://public.example/chat",
                auth_type=AuthType.NONE,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                lifecycle_status=AgentVersionLifecycle.ACTIVE,
                enabled=True,
            )
            session.add(version)
            await session.flush()
            owner_id = owner.id
            public_version_id = version.id
    finally:
        await engine.dispose()

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app, settings, owner_id, public_version_id


async def login_owner(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/user-login",
        headers={"Origin": "http://test"},
        json={"username": "stream.owner", "password": OWNER_PASSWORD},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("agent_eval_csrf")
    assert csrf is not None
    return {"Origin": "http://test", "X-CSRF-Token": csrf}


async def mark_attempt_running(app: FastAPI, attempt_id: UUID, owner_id: str) -> WorkspaceClaim:
    token = uuid4().hex
    async with session_scope(app.state.session_factory) as session:
        attempt = await session.get(WorkspaceAttempt, attempt_id)
        assert attempt is not None
        attempt.status = WorkspaceAttemptStatus.RUNNING
        attempt.claim_owner_id = owner_id
        attempt.claim_token = token
        attempt.claimed_at = utc_now()
        attempt.started_at = utc_now()
        attempt.heartbeat_at = utc_now()
        attempt.lease_expires_at = utc_now() + timedelta(minutes=5)
    return WorkspaceClaim(attempt_id=attempt_id, claim_token=token)


def streaming_config() -> InferenceConfig:
    return InferenceConfig(
        endpoint_url="https://api.test/v1/chat/completions",
        auth_type=AuthType.BEARER,
        token=API_KEY,
        request_template=(
            '{"model":"m","messages":[{"role":"user","content":"{{prompt}}"}],'
            '"stream":false}'
        ),
        answer_jsonpath="$.choices[0].message.content",
        citations_jsonpath=None,
        timeout_seconds=30,
        supports_streaming=True,
    )


def sse_lines(*payloads: str) -> bytes:
    return "".join(f"data: {payload}\n\n" for payload in payloads).encode()


async def test_partial_answer_writer_throttles_and_flushes() -> None:
    writes: list[str] = []
    now = [0.0]

    async def persist(text: str) -> None:
        writes.append(text)

    writer = PartialAnswerWriter(persist, interval=0.5, clock=lambda: now[0])
    await writer.write("a")
    now[0] = 0.1
    await writer.write("ab")
    now[0] = 0.2
    await writer.write("abc")
    assert writes == ["a"]
    await writer.flush()
    assert writes == ["a", "abc"]
    now[0] = 0.8
    await writer.write("abcd")
    await writer.flush()
    assert writes == ["a", "abc", "abcd"]


async def test_streaming_inference_accumulates_sse_deltas() -> None:
    partials: list[str] = []
    finished: list[AttemptCompletion] = []

    async def start_attempt(attempt_no: int) -> UUID:
        assert attempt_no == 1
        return uuid4()

    async def finish_attempt(completion: AttemptCompletion) -> None:
        finished.append(completion)

    async def on_partial(text: str) -> None:
        partials.append(text)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        content = sse_lines(
            '{"choices":[{"delta":{"content":"Hello"}}]}',
            '{"choices":[{"delta":{"content":" world"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
            "[DONE]",
        )
        return httpx.Response(
            200, content=content, headers={"content-type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await execute_streaming_inference(
            client,
            streaming_config(),
            "hi",
            previous_attempt_count=0,
            start_attempt=start_attempt,
            finish_attempt=finish_attempt,
            on_partial=on_partial,
        )
    assert outcome.success
    assert outcome.answer == "Hello world"
    assert outcome.citations == []
    assert partials == ["Hello", "Hello world"]
    assert len(finished) == 1
    assert finished[0].input_tokens == 3
    assert finished[0].output_tokens == 2
    assert finished[0].total_tokens == 5


async def test_streaming_inference_classifies_errors() -> None:
    async def start_attempt(attempt_no: int) -> UUID:
        return uuid4()

    async def finish_attempt(completion: AttemptCompletion) -> None:
        pass

    async def on_partial(text: str) -> None:
        raise AssertionError(f"unexpected partial {text}")

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unauthorized)) as client:
        outcome = await execute_streaming_inference(
            client,
            streaming_config(),
            "hi",
            previous_attempt_count=0,
            start_attempt=start_attempt,
            finish_attempt=finish_attempt,
            on_partial=on_partial,
            sleep=lambda seconds: asyncio.sleep(0),
        )
    assert not outcome.success
    assert outcome.error_type == "agent_authentication"

    def stream_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_lines('{"error":{"message":"model overloaded server_error"}}'),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(stream_error)) as client:
        outcome = await execute_streaming_inference(
            client,
            streaming_config(),
            "hi",
            previous_attempt_count=0,
            start_attempt=start_attempt,
            finish_attempt=finish_attempt,
            on_partial=on_partial,
            sleep=lambda seconds: asyncio.sleep(0),
        )
    assert not outcome.success
    assert outcome.error_type == "agent_server_error"


async def test_workspace_streaming_attempt_publishes_and_clears_partial(
    tmp_path: Path,
) -> None:
    async with streaming_client(tmp_path) as (client, app, settings, _, _):
        headers = await login_owner(client)
        attempt_holder: dict[str, UUID] = {}
        observed_partials: list[str | None] = []

        async def sse_body() -> AsyncIterator[bytes]:
            yield sse_lines('{"choices":[{"delta":{"content":"Hello"}}]}')
            # The throttled writer uses interval 0 in this test, so the partial
            # must be visible in the database before the next chunk arrives.
            async with session_scope(app.state.session_factory) as session:
                attempt = await session.get(WorkspaceAttempt, attempt_holder["id"])
                observed_partials.append(attempt.partial_answer if attempt else None)
            yield sse_lines(
                '{"choices":[{"delta":{"content":" world"}}]}',
                "[DONE]",
            )

        def provider(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("stream") is True:
                return httpx.Response(
                    200,
                    content=sse_body(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        app.state.agent_http_transport = httpx.MockTransport(provider)
        created = await client.post(
            "/api/v1/workspace/private-agents",
            headers=headers,
            json={
                "name": "Streaming Agent",
                "preset": "openai",
                "model": "gpt-4o-mini",
                "api_key": API_KEY,
            },
        )
        assert created.status_code == 201
        promoted = await client.post(
            f"/api/v1/workspace/private-agents/{created.json()['agent_id']}/test",
            headers=headers,
        )
        assert promoted.status_code == 200
        version_id = promoted.json()["agent_version_id"]

        conversation = await client.post(
            "/api/v1/workspace/conversations",
            headers=headers,
            json={"agent_version_id": version_id},
        )
        assert conversation.status_code == 201
        sent = await client.post(
            f"/api/v1/workspace/conversations/{conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "Hello", "expected_revision": 0},
        )
        assert sent.status_code == 202
        attempt_id = UUID(sent.json()["attempt"]["id"])
        attempt_holder["id"] = attempt_id

        claim = await mark_attempt_running(app, attempt_id, "stream-worker")
        processed = await process_workspace_attempt(
            app.state.session_factory,
            settings,
            claim,
            artifact_store=app.state.artifact_store,
            token_cipher=app.state.token_cipher,
            owner_id="stream-worker",
            limits=WorkerLimits.from_settings(settings),
            transport=httpx.MockTransport(provider),
            partial_write_interval=0,
        )
        assert processed
        assert observed_partials == ["Hello"]
        async with session_scope(app.state.session_factory) as session:
            attempt = await session.get(WorkspaceAttempt, attempt_id)
            assert attempt is not None
            assert attempt.status == WorkspaceAttemptStatus.SUCCEEDED
            # Terminal state: the answer lives on the assistant message and the
            # partial column is cleared.
            assert attempt.partial_answer is None
            assert attempt.assistant_message_id is not None
            message = await session.get(WorkspaceMessage, attempt.assistant_message_id)
            assert message is not None
            assert message.content == "Hello world"


async def test_attempt_stream_endpoint_events_and_auth(tmp_path: Path) -> None:
    async with streaming_client(tmp_path) as (client, app, _, owner_id, public_version_id):
        unauthenticated = await client.get(f"/api/v1/workspace/attempts/{uuid4()}/stream")
        assert unauthenticated.status_code == 401

        await login_owner(client)
        missing = await client.get(f"/api/v1/workspace/attempts/{uuid4()}/stream")
        assert missing.status_code == 404

        async with session_scope(app.state.session_factory) as session:
            conversation = WorkspaceConversation(
                owner_user_id=owner_id,
                pinned_agent_version_id=public_version_id,
                title="Stream",
                last_turn_no=1,
            )
            session.add(conversation)
            await session.flush()
            user_message = WorkspaceMessage(
                conversation_id=conversation.id,
                turn_no=1,
                role=WorkspaceMessageRole.USER,
                variant_no=1,
                active=True,
                content="Hello",
            )
            session.add(user_message)
            await session.flush()
            attempt = WorkspaceAttempt(
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                agent_version_id=public_version_id,
                generation_no=1,
                idempotency_key=str(uuid4()),
                request_fingerprint="a" * 64,
                status=WorkspaceAttemptStatus.RUNNING,
                prompt_hash="b" * 64,
                prompt_characters=10,
                prompt_format="workspace/v1",
                partial_answer="Hel",
                claim_owner_id="worker-1",
                claim_token=uuid4().hex,
                claimed_at=utc_now(),
                started_at=utc_now(),
                heartbeat_at=utc_now(),
                lease_expires_at=utc_now() + timedelta(minutes=5),
            )
            session.add(attempt)
            await session.flush()
            attempt_id = attempt.id
            conversation_id = conversation.id
            stream_url = f"/api/v1/workspace/attempts/{attempt_id}/stream"

        async def complete_attempt() -> None:
            await asyncio.sleep(0.12)
            async with session_scope(app.state.session_factory) as session:
                attempt = await session.get(WorkspaceAttempt, attempt_id)
                assert attempt is not None
                attempt.partial_answer = "Hello"
            await asyncio.sleep(0.12)
            async with session_scope(app.state.session_factory) as session:
                attempt = await session.get(WorkspaceAttempt, attempt_id)
                assert attempt is not None
                assistant = WorkspaceMessage(
                    conversation_id=conversation_id,
                    turn_no=1,
                    role=WorkspaceMessageRole.ASSISTANT,
                    variant_no=1,
                    active=True,
                    content="Hello world",
                )
                session.add(assistant)
                await session.flush()
                attempt.assistant_message_id = assistant.id
                attempt.status = WorkspaceAttemptStatus.SUCCEEDED
                attempt.partial_answer = None
                attempt.claim_owner_id = None
                attempt.claim_token = None
                attempt.lease_expires_at = None
                attempt.heartbeat_at = None
                attempt.finished_at = utc_now()

        completer = asyncio.create_task(complete_attempt())
        try:
            async with asyncio.timeout(10):
                async with client.stream("GET", stream_url) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert response.headers["x-accel-buffering"] == "no"
                    body = await response.aread()
        finally:
            await completer

        events = body.decode().split("\n\n")
        partials = [
            json.loads(block.split("data: ", 1)[1])["text"]
            for block in events
            if block.startswith("event: partial")
        ]
        done = [
            json.loads(block.split("data: ", 1)[1])
            for block in events
            if block.startswith("event: done")
        ]
        assert partials[0] == "Hel"
        assert "Hello" in partials
        assert len(done) == 1
        assert done[0]["status"] == "succeeded"
        assert done[0]["answer"] == "Hello world"
        assert done[0]["error"] is None
