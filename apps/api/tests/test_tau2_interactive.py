"""Tests for tau2-bench interactive evaluation (Phase 1).

All tests are skipped when the tau2 dependency group is not installed. The
mock domain shipped by tau2 is driven through a local scripted HTTP server and
a deterministic fake user simulator, so no real LLM calls happen.
"""

import asyncio
import json
import os
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

# TAU2_DATA_DIR is resolved when tau2 is first imported, so point it at a
# throwaway directory (populated by the mock_domain_data fixture) before any
# tau2 import can happen.
_TAU2_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="tau2-test-data-"))
os.environ["TAU2_DATA_DIR"] = str(_TAU2_TEST_DATA_DIR)

tau2 = pytest.importorskip("tau2")

import httpx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from tau2.data_model.message import AssistantMessage, UserMessage  # noqa: E402
from tau2.registry import registry  # noqa: E402

from agent_eval.agents.inference import InferenceConfig  # noqa: E402
from agent_eval.agents.interactive import (  # noqa: E402
    STOP_TOKEN,
    AgentProtocolError,
    HttpTextProtocolAgent,
    parse_protocol_action,
)
from agent_eval.core.settings import Settings  # noqa: E402
from agent_eval.datasets.schema import QuestionRecord, records_content_hash  # noqa: E402
from agent_eval.datasets.tau2_importer import (  # noqa: E402
    TAU2_RETAIL_BENCHMARK,
    TAU2_VERSION,
    build_tau2_retail_records,
    import_tau2_retail,
)
from agent_eval.db.engine import create_engine  # noqa: E402
from agent_eval.db.enums import Ability, AuthType, QuestionType  # noqa: E402
from agent_eval.db.session import create_session_factory, session_scope  # noqa: E402
from agent_eval.runs.profiles import (  # noqa: E402
    TAU2_RETAIL_PROFILE_KEY,
    available_run_profiles,
    build_run_profiles,
    default_profile_key,
)
from agent_eval.worker.interactive_executor import (  # noqa: E402
    InteractiveExecutionError,
    parse_task_ref,
    run_tau2_episode,
)
from agent_eval.worker.scoring import ScoreWork, local_score  # noqa: E402

MOCK_TASK_IDS = ["mock-1", "mock-2"]


@pytest.fixture(scope="module", autouse=True)
def mock_domain_data() -> None:
    root = _TAU2_TEST_DATA_DIR / "tau2" / "domains" / "mock"
    root.mkdir(parents=True, exist_ok=True)
    (root / "db.json").write_text(json.dumps({"tasks": {}, "users": {}}))
    (root / "user_db.json").write_text(json.dumps({"notifications": {}}))
    (root / "policy.md").write_text("# Mock policy\nAlways be helpful and concise.\n")
    tasks = [
        {
            "id": task_id,
            "user_scenario": {"instructions": f"Instructions for {task_id}."},
        }
        for task_id in MOCK_TASK_IDS
    ]
    (root / "tasks.json").write_text(json.dumps(tasks))
    (root / "split_tasks.json").write_text(json.dumps({"base": MOCK_TASK_IDS}))


def make_config(endpoint_url: str) -> InferenceConfig:
    return InferenceConfig(
        endpoint_url=endpoint_url,
        auth_type=AuthType.NONE,
        token=None,
        request_template='{"query":"{{prompt}}"}',
        answer_jsonpath="$.answer",
        citations_jsonpath=None,
        timeout_seconds=10,
    )


def fenced(payload: dict) -> str:
    return f"```json\n{json.dumps(payload)}\n```"


def mock_tools_and_policy() -> tuple[list, str]:
    environment = registry.get_env_constructor("mock")()
    return environment.get_tools(), environment.get_policy()


def mock_transport_agent(replies: list[str]) -> tuple[HttpTextProtocolAgent, list[dict]]:
    recorded: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(json.loads(request.content))
        reply = replies[min(len(recorded) - 1, len(replies) - 1)]
        return httpx.Response(200, json={"answer": reply})

    tools, policy = mock_tools_and_policy()
    agent = HttpTextProtocolAgent(
        tools=tools,
        domain_policy=policy,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config=make_config("http://agent.test/run"),
    )
    return agent, recorded


class FakeUserSimulator:
    """Deterministic user: sends its instructions once, then stops."""

    def __init__(self, llm=None, instructions=None, tools=None, llm_args=None) -> None:
        self._instructions = str(instructions or "Please help me.")
        self._replied = False

    def get_init_state(self, message_history=None) -> list:
        return list(message_history or [])

    def generate_next_message(self, message, state):
        history = list(state)
        if message is not None:
            history.append(message)
        content = "###STOP###" if self._replied else self._instructions
        self._replied = True
        reply = UserMessage(role="user", content=content)
        history.append(reply)
        return reply, history


class scripted_agent_server:
    """Local HTTP server answering agent prompts with scripted replies."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.requests: list[dict] = []

    def __enter__(self) -> "scripted_agent_server":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(length)))
                reply = outer._replies[min(len(outer.requests) - 1, len(outer._replies) - 1)]
                payload = json.dumps({"answer": reply}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def episode_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/episode.db",
        data_dir=tmp_path,
        deepseek_api_key="sk-test",
    )


def run_episode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: list[str],
                task_id: str = "mock-1"):
    monkeypatch.setattr("tau2.user.user_simulator.UserSimulator", FakeUserSimulator)
    with scripted_agent_server(replies) as server:
        result = run_tau2_episode(
            config=make_config(server.url),
            task_ref=f"tau2:mock:{task_id}",
            trial_index=0,
            timeout_seconds=60,
            settings=episode_settings(tmp_path),
        )
    return result


# ---------------------------------------------------------------------------
# Protocol parsing
# ---------------------------------------------------------------------------


def test_parse_protocol_actions() -> None:
    respond = parse_protocol_action(
        fenced({"action": "respond", "content": "How can I help?"}),
        tool_names={"get_users"},
    )
    assert respond.action == "respond" and respond.content == "How can I help?"

    tool = parse_protocol_action(
        fenced({"action": "tool", "name": "get_users", "arguments": {}}),
        tool_names={"get_users"},
    )
    assert tool.action == "tool" and tool.tool_name == "get_users" and tool.tool_arguments == {}

    stop = parse_protocol_action(fenced({"action": "stop"}), tool_names=set())
    assert stop.action == "stop"


@pytest.mark.parametrize(
    "answer",
    [
        "no fenced block at all",
        "```json\nnot json\n```",
        "```json\n[1, 2]\n```",
        fenced({"action": "unknown"}),
        fenced({"action": "respond", "content": ""}),
        fenced({"action": "respond"}),
        fenced({"action": "tool", "name": "missing_tool", "arguments": {}}),
        fenced({"action": "tool", "name": "get_users", "arguments": []}),
    ],
)
def test_parse_protocol_action_rejections(answer: str) -> None:
    from agent_eval.agents.interactive import ProtocolParseError

    with pytest.raises(ProtocolParseError):
        parse_protocol_action(answer, tool_names={"get_users"})


# ---------------------------------------------------------------------------
# HttpTextProtocolAgent
# ---------------------------------------------------------------------------


def test_agent_respond_and_tool_actions() -> None:
    agent, recorded = mock_transport_agent(
        [
            fenced({"action": "tool", "name": "get_users", "arguments": {}}),
            fenced({"action": "respond", "content": "Here is what I found."}),
        ]
    )
    state = agent.get_init_state()

    first, state = agent.generate_next_message(UserMessage(role="user", content="hi"), state)
    assert isinstance(first, AssistantMessage) and first.is_tool_call()
    assert first.tool_calls is not None and first.tool_calls[0].name == "get_users"
    assert first.tool_calls[0].arguments == {}
    assert first.cost == 0.0 and first.usage is not None

    second, state = agent.generate_next_message(first, state)
    assert not second.is_tool_call() and second.content == "Here is what I found."
    assert not HttpTextProtocolAgent.is_stop(second)
    assert len(recorded) == 2
    # Every turn renders the domain policy and the transcript into the prompt.
    assert "Mock policy" in recorded[0]["query"]
    assert "user: hi" in recorded[0]["query"]


def test_agent_stop_action() -> None:
    agent, _ = mock_transport_agent([fenced({"action": "stop"})])
    message, _ = agent.generate_next_message(
        UserMessage(role="user", content="done"), agent.get_init_state()
    )
    assert message.content == STOP_TOKEN
    assert HttpTextProtocolAgent.is_stop(message)


def test_agent_correction_retries_once_then_succeeds() -> None:
    agent, recorded = mock_transport_agent(
        ["this is not protocol JSON", fenced({"action": "respond", "content": "sorry, ok"})]
    )
    message, _ = agent.generate_next_message(
        UserMessage(role="user", content="hi"), agent.get_init_state()
    )
    assert message.content == "sorry, ok"
    assert len(recorded) == 2
    assert "Correction" in recorded[1]["query"]


def test_agent_protocol_error_after_second_invalid_reply() -> None:
    agent, recorded = mock_transport_agent(["garbage one"])
    with pytest.raises(AgentProtocolError):
        agent.generate_next_message(
            UserMessage(role="user", content="hi"), agent.get_init_state()
        )
    assert len(recorded) == 2


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def interactive_work(
    *, task_reward: Decimal | None, failed: bool = False
) -> ScoreWork:
    return ScoreWork(
        item_id=uuid4(),
        ability=Ability.AGENTIC,
        question_type=QuestionType.INTERACTIVE,
        question="instructions",
        reference_answer="tau2:retail:task-1",
        candidate_answer=None,
        valid_labels=[],
        requires_citations=False,
        failed=failed,
        task_reward=task_reward,
        reward_summary="reward=0.5000; termination=agent_stop",
    )


def test_interactive_scoring_maps_reward_to_answer_score() -> None:
    result = local_score(interactive_work(task_reward=Decimal("0.5")))
    assert result.answer_score == 50
    assert result.failed is False
    assert result.judge_provider == "tau2"
    assert result.judge_prompt_version is not None
    assert result.judge_prompt_version.startswith("tau2-")
    assert result.judge_details is not None
    assert result.judge_details["tau2"]["domain"] == "retail"
    assert result.judge_details["tau2"]["task_reward"] == 0.5

    zero = local_score(interactive_work(task_reward=None))
    assert zero.answer_score == 0 and zero.failed is False


def test_interactive_scoring_failed_item_stays_failed() -> None:
    result = local_score(interactive_work(task_reward=Decimal("1.0"), failed=True))
    assert result.failed is True and result.answer_score == 0


def test_parse_task_ref() -> None:
    assert parse_task_ref("tau2:retail:task-1") == ("retail", "task-1")
    for invalid in ["retail:task-1", "tau2:retail", "tau2::task-1", "tau2:retail:"]:
        with pytest.raises(InteractiveExecutionError):
            parse_task_ref(invalid)


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


def fake_tau2_tasks(ids: list[str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            id=task_id,
            user_scenario=SimpleNamespace(instructions=f"Instructions for {task_id}."),
        )
        for task_id in ids
    ]


def patch_tasks_loader(monkeypatch: pytest.MonkeyPatch, ids: list[str]) -> None:
    monkeypatch.setattr(
        registry,
        "get_tasks_loader",
        lambda domain: (lambda split: fake_tau2_tasks(ids)),
    )


def test_tau2_importer_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_tasks_loader(monkeypatch, ["b2", "a1", "c3"])
    records = build_tau2_retail_records(tmp_path)

    assert [record.external_id for record in records] == ["retail-a1", "retail-b2", "retail-c3"]
    for record in records:
        task_id = record.external_id.removeprefix("retail-")
        assert record.benchmark == TAU2_RETAIL_BENCHMARK
        assert record.ability == Ability.AGENTIC
        assert record.type == QuestionType.INTERACTIVE
        assert record.reference_answer == f"tau2:retail:{task_id}"
        assert record.source_version == f"tau2-{TAU2_VERSION}"
        assert record.requires_citations is False

    limited = build_tau2_retail_records(tmp_path, limit=2)
    assert [record.external_id for record in limited] == ["retail-a1", "retail-b2"]


def test_tau2_content_hash_includes_tau2_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_tasks_loader(monkeypatch, ["a1"])
    records = build_tau2_retail_records(tmp_path)
    mutated: list[QuestionRecord] = [
        record.model_copy(update={"source_version": "tau2-0.0.0"}) for record in records
    ]
    assert records_content_hash(records) != records_content_hash(mutated)


# ---------------------------------------------------------------------------
# Run profile
# ---------------------------------------------------------------------------


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def import_fake_tau2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_ids: list[str]
):
    patch_tasks_loader(monkeypatch, task_ids)
    database_path = tmp_path / "tau2.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            before = await build_run_profiles(session)
        async with session_scope(session_factory) as session:
            imported = await import_tau2_retail(
                session, data_dir=tmp_path, limit=len(task_ids)
            )
        async with session_scope(session_factory) as session:
            after = await build_run_profiles(session)
    finally:
        await engine.dispose()
    return imported, before, after


async def test_tau2_retail_profile_appears_after_complete_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task_ids = [f"task-{index}" for index in range(114)]
    imported, before, after = await import_fake_tau2(tmp_path, monkeypatch, task_ids)

    assert TAU2_RETAIL_PROFILE_KEY not in {profile.key for profile in before}
    assert len(imported) == 1 and imported[0].question_count == 114

    profile = next(item for item in after if item.key == TAU2_RETAIL_PROFILE_KEY)
    assert profile.question_count == 114
    assert profile.score_config_id is None
    assert profile.ranking_eligible is False
    assert profile.publishes_to == ()
    # tau2-retail must not steal the default profile.
    assert default_profile_key(after) == default_profile_key(before) != TAU2_RETAIL_PROFILE_KEY
    assert TAU2_RETAIL_PROFILE_KEY in {
        item.key for item in available_run_profiles(after)
    }


async def test_tau2_retail_profile_hidden_when_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task_ids = [f"task-{index}" for index in range(113)]
    _, _, after = await import_fake_tau2(tmp_path, monkeypatch, task_ids)
    assert TAU2_RETAIL_PROFILE_KEY not in {profile.key for profile in after}


# ---------------------------------------------------------------------------
# Full episodes (mock domain + fake user simulator + scripted HTTP agent)
# ---------------------------------------------------------------------------


def test_episode_respond_then_user_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_episode(
        tmp_path,
        monkeypatch,
        [fenced({"action": "respond", "content": "All taken care of."})],
    )
    assert result.success is True and result.error_type is None
    assert result.task_reward == Decimal("1.0")
    assert result.trajectory is not None
    assert result.trajectory["termination_reason"] == "user_stop"
    assert result.trajectory["agent_protocol"] == "http-text-json-v1"
    assert "reward=1.0000" in (result.answer or "")


def test_episode_tool_call_then_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = run_episode(
        tmp_path,
        monkeypatch,
        [
            fenced({"action": "tool", "name": "get_users", "arguments": {}}),
            fenced({"action": "respond", "content": "There are no users."}),
        ],
    )
    assert result.success is True and result.error_type is None
    assert result.task_reward == Decimal("1.0")
    assert result.trajectory is not None
    assert result.trajectory["termination_reason"] == "user_stop"
    roles = [message["role"] for message in result.trajectory["messages"]]
    assert "tool" in roles


def test_episode_agent_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = run_episode(tmp_path, monkeypatch, [fenced({"action": "stop"})])
    assert result.success is True and result.error_type is None
    assert result.task_reward == Decimal("1.0")
    assert result.trajectory is not None
    assert result.trajectory["termination_reason"] == "agent_stop"


def test_episode_agent_protocol_error_scores_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_episode(tmp_path, monkeypatch, ["complete nonsense"])
    # Protocol failures are recorded as a scored trial with reward 0, not as a
    # failed execution item.
    assert result.success is True
    assert result.error_type == "agent_protocol_error"
    assert result.task_reward == Decimal("0")
    assert result.trajectory is not None
    assert result.trajectory["termination_reason"] == "agent_error"
    assert result.trajectory["agent_protocol_error"]


def test_episode_unknown_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tau2.user.user_simulator.UserSimulator", FakeUserSimulator)
    with pytest.raises(InteractiveExecutionError, match="interactive_task_missing"):
        run_tau2_episode(
            config=make_config("http://127.0.0.1:1/"),
            task_ref="tau2:mock:missing-task",
            trial_index=0,
            timeout_seconds=60,
            settings=episode_settings(tmp_path),
        )


def test_episode_invalid_task_ref(tmp_path: Path) -> None:
    with pytest.raises(InteractiveExecutionError, match="interactive_task_ref_invalid"):
        run_tau2_episode(
            config=make_config("http://127.0.0.1:1/"),
            task_ref="bogus",
            trial_index=0,
            timeout_seconds=60,
            settings=episode_settings(tmp_path),
        )
