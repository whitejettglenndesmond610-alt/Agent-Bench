"""Tests for MCPMark filesystem (MCP) evaluation (M1).

All tests are skipped when the ``mcp`` dependency group is not installed. The
episode tests drive an in-memory MCP server pair (mcp SDK ``Client(server)``)
and a scripted HTTP agent, so no real npx subprocess or network is involved.
"""

import asyncio
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

mcp = pytest.importorskip("mcp")

import httpx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agent_eval.agents.inference import InferenceConfig  # noqa: E402
from agent_eval.agents.protocol import (  # noqa: E402
    ProtocolParseError,
    build_correction_message,
    parse_mcp_action,
)
from agent_eval.core.settings import Settings  # noqa: E402
from agent_eval.datasets.importer import MCP_V1_COHORT_KEY  # noqa: E402
from agent_eval.datasets.mcpmark_importer import (  # noqa: E402
    COMMIT_PIN_FILE,
    MCPMARK_FILESYSTEM_BENCHMARK,
    build_mcpmark_filesystem_records,
    import_mcpmark_filesystem,
    mcpmark_commit_pin,
)
from agent_eval.db.engine import create_engine  # noqa: E402
from agent_eval.db.enums import Ability, AgentStatus, AuthType, QuestionType, RunScope  # noqa: E402
from agent_eval.db.models import (  # noqa: E402
    Agent,
    AgentVersion,
    DatasetVersion,
    JudgeConfig,
)
from agent_eval.db.session import create_session_factory, session_scope  # noqa: E402
from agent_eval.runs.profiles import (  # noqa: E402
    MCP_FILESYSTEM_PROFILE_KEY,
    available_run_profiles,
    build_run_profiles,
    default_profile_key,
)
from agent_eval.runs.service import (  # noqa: E402
    RunConfigurationError,
    prepare_evaluation_run,
)
from agent_eval.worker.executor import WorkItem, route_interactive_items  # noqa: E402
from agent_eval.worker.mcp_executor import (  # noqa: E402
    McpExecutionError,
    McpWorkItem,
    ensure_state_zip,
    execute_mcp_items,
    extract_state_zip,
    parse_mcp_task_ref,
    run_mcp_episode,
    run_verify_script,
)
from agent_eval.worker.result_writer import AttemptCompletion  # noqa: E402

FAKE_COMMIT = "0" * 40


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


def make_task_dir(
    repo: Path,
    *,
    suite: str = "easy",
    category: str = "file_context",
    task_id: str = "task_one",
    state_url: str | None = None,
    verify_body: str | None = None,
) -> Path:
    task_dir = repo / "tasks" / "filesystem" / suite / category / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "task_id": task_id,
        "category_id": category,
        "mcp": ["filesystem"],
        "meta_data": {"stateType": "zip", "stateUrl": state_url},
    }
    (task_dir / "meta.json").write_text(json.dumps(meta))
    (task_dir / "description.md").write_text(f"# Task {task_id}\nDo the thing.\n")
    (task_dir / "verify.py").write_text(
        verify_body
        or (
            "import os, sys\n"
            "from pathlib import Path\n"
            "root = Path(os.environ['FILESYSTEM_TEST_DIR'])\n"
            "sys.exit(0 if (root / 'answer.txt').is_file() else 1)\n"
        )
    )
    return task_dir


def make_state_zip(path: Path, *, wrapped_dir: str | None = "file_context") -> Path:
    zip_path = path / "state.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        prefix = f"{wrapped_dir}/" if wrapped_dir else ""
        archive.writestr(f"{prefix}note.txt", "hello")
    return zip_path


# ---------------------------------------------------------------------------
# Protocol parsing (shared helpers reused from agents/protocol.py)
# ---------------------------------------------------------------------------


def test_parse_mcp_actions() -> None:
    tool = parse_mcp_action(
        fenced({"action": "tool", "name": "write_file", "arguments": {"path": "a.txt"}}),
        tool_names={"write_file"},
    )
    assert tool.action == "tool"
    assert tool.tool_name == "write_file"
    assert tool.tool_arguments == {"path": "a.txt"}

    stop = parse_mcp_action(
        fenced({"action": "stop", "answer": "done"}), tool_names={"write_file"}
    )
    assert stop.action == "stop" and stop.answer == "done"

    bare_stop = parse_mcp_action(fenced({"action": "stop"}), tool_names=set())
    assert bare_stop.action == "stop" and bare_stop.answer is None


@pytest.mark.parametrize(
    "answer",
    [
        "no fenced block at all",
        "```json\nnot json\n```",
        "```json\n[1, 2]\n```",
        fenced({"action": "respond", "content": "hi"}),  # tau2-only action
        fenced({"action": "tool", "name": "missing", "arguments": {}}),
        fenced({"action": "tool", "name": "write_file", "arguments": []}),
        fenced({"action": "stop", "answer": 42}),
    ],
)
def test_parse_mcp_action_rejections(answer: str) -> None:
    with pytest.raises(ProtocolParseError):
        parse_mcp_action(answer, tool_names={"write_file"})


def test_build_correction_message() -> None:
    error = ProtocolParseError("bad reply")
    correction = build_correction_message(error, "x" * 2000)
    assert "bad reply" in correction
    assert "x" * 1000 in correction
    assert "x" * 1001 not in correction


# ---------------------------------------------------------------------------
# task_ref parsing and executor routing
# ---------------------------------------------------------------------------


def test_parse_mcp_task_ref() -> None:
    assert parse_mcp_task_ref("mcp:filesystem:easy/file_context/task_one") == (
        "easy",
        "file_context",
        "task_one",
    )


@pytest.mark.parametrize(
    "task_ref",
    [
        "tau2:retail:1",
        "mcp:postgres:easy/x/y",
        "mcp:filesystem:easy",
        "mcp:filesystem:easy/cat/task/extra",
        "mcp:filesystem:easy//task",
        "mcp:filesystem:",
        "",
    ],
)
def test_parse_mcp_task_ref_rejections(task_ref: str) -> None:
    with pytest.raises(McpExecutionError, match="mcp_task_ref_invalid"):
        parse_mcp_task_ref(task_ref)


def test_route_interactive_items() -> None:
    def item(reference: str) -> WorkItem:
        return WorkItem(
            id=uuid4(),
            run_agent_id=uuid4(),
            question_id=uuid4(),
            attempt_count=0,
            prompt="p",
            question_type=QuestionType.INTERACTIVE,
            reference_answer=reference,
            trial_index=0,
        )

    tau2_items, mcp_items = route_interactive_items(
        [
            item("tau2:retail:1"),
            item("mcp:filesystem:easy/cat/task"),
            item("tau2:airline:2"),
            item("mcp:filesystem:easy/cat/task2"),
        ]
    )
    assert [i.reference_answer for i in tau2_items] == ["tau2:retail:1", "tau2:airline:2"]
    assert [i.reference_answer for i in mcp_items] == [
        "mcp:filesystem:easy/cat/task",
        "mcp:filesystem:easy/cat/task2",
    ]


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


def make_fake_repo(tmp_path: Path, *, tasks: int = 2, commit: str = FAKE_COMMIT) -> Path:
    repo = tmp_path / "mcpmark"
    repo.mkdir(parents=True)
    (repo / COMMIT_PIN_FILE).write_text(commit)
    for index in range(tasks):
        make_task_dir(
            repo,
            category="file_context",
            task_id=f"task_{index:02d}",
            state_url=f"https://storage.mcpmark.ai/filesystem/file_context_{index}.zip",
        )
    return repo


def test_build_mcpmark_records(tmp_path: Path) -> None:
    repo = make_fake_repo(tmp_path)
    records = build_mcpmark_filesystem_records(repo, "easy")
    assert len(records) == 2
    record = records[0]
    assert record.benchmark == MCPMARK_FILESYSTEM_BENCHMARK
    assert record.external_id == "easy/file_context/task_00"
    assert record.reference_answer == "mcp:filesystem:easy/file_context/task_00"
    assert record.prompt.startswith("# Task task_00")
    assert record.type == QuestionType.INTERACTIVE
    assert record.ability == Ability.AGENTIC
    assert record.source_version == f"mcpmark-{FAKE_COMMIT}"
    assert record.domain == "filesystem/file_context"


def test_build_mcpmark_records_limit_and_suite(tmp_path: Path) -> None:
    repo = make_fake_repo(tmp_path, tasks=3)
    assert len(build_mcpmark_filesystem_records(repo, "easy", limit=2)) == 2
    with pytest.raises(ValueError, match="unsupported MCPMark suite"):
        build_mcpmark_filesystem_records(repo, "hard")
    with pytest.raises(ValueError, match="no MCPMark filesystem/standard tasks"):
        build_mcpmark_filesystem_records(repo, "standard")


def test_mcpmark_commit_pin_requires_pin(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    with pytest.raises(ValueError, match="cannot pin an MCPMark commit"):
        mcpmark_commit_pin(repo)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


async def import_fake_mcpmark(tmp_path: Path, *, tasks: int = 10, commit: str = FAKE_COMMIT):
    repo = make_fake_repo(tmp_path, tasks=tasks, commit=commit)
    database_path = tmp_path / "mcp.db"
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
            imported = await import_mcpmark_filesystem(session, repo_dir=repo, suite="easy")
        async with session_scope(session_factory) as session:
            after = await build_run_profiles(session)
            versions = list(await session.scalars(select(DatasetVersion)))
            cohorts = {version.cohort_key for version in versions}
            hashes = {version.content_hash for version in versions}
    finally:
        await engine.dispose()
    return imported, before, after, cohorts, hashes


async def test_mcpmark_import_and_profile(tmp_path: Path) -> None:
    imported, before, after, cohorts, _ = await import_fake_mcpmark(tmp_path)
    assert len(imported) == 1 and imported[0].question_count == 10
    assert imported[0].created is True
    assert cohorts == {MCP_V1_COHORT_KEY}

    assert MCP_FILESYSTEM_PROFILE_KEY not in {profile.key for profile in before}
    profile = next(item for item in after if item.key == MCP_FILESYSTEM_PROFILE_KEY)
    assert profile.question_count == 10
    assert profile.score_config_id is None
    assert profile.ranking_eligible is False
    assert profile.publishes_to == ()
    assert default_profile_key(after) == default_profile_key(before)
    assert MCP_FILESYSTEM_PROFILE_KEY in {
        item.key for item in available_run_profiles(after)
    }


async def test_mcpmark_profile_hidden_when_incomplete(tmp_path: Path) -> None:
    _, _, after, _, _ = await import_fake_mcpmark(tmp_path, tasks=9)
    assert MCP_FILESYSTEM_PROFILE_KEY not in {profile.key for profile in after}


async def test_mcpmark_content_hash_tracks_commit_pin(tmp_path: Path) -> None:
    hashes_a = (await import_fake_mcpmark(tmp_path / "a", commit="a" * 40))[4]
    hashes_b = (await import_fake_mcpmark(tmp_path / "b", commit="b" * 40))[4]
    assert hashes_a != hashes_b


# ---------------------------------------------------------------------------
# State zip handling and verify.py scoring
# ---------------------------------------------------------------------------


async def test_ensure_state_zip_local_file(tmp_path: Path) -> None:
    zip_path = make_state_zip(tmp_path)
    resolved = await ensure_state_zip(
        f"file://{zip_path}", task_dir=tmp_path, cache_dir=tmp_path / "cache"
    )
    assert resolved == zip_path
    with pytest.raises(McpExecutionError, match="mcp_state_missing"):
        await ensure_state_zip(
            "file:///nonexistent.zip", task_dir=tmp_path, cache_dir=tmp_path / "cache"
        )


def test_extract_state_zip_unwraps_single_directory(tmp_path: Path) -> None:
    zip_path = make_state_zip(tmp_path, wrapped_dir="file_context")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    test_dir = extract_state_zip(zip_path, workspace)
    assert test_dir == workspace / "file_context"
    assert (test_dir / "note.txt").read_text() == "hello"


def test_extract_state_zip_ignores_macosx_junk(tmp_path: Path) -> None:
    zip_path = tmp_path / "state.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("papers/paper_1.html", "<html/>")
        archive.writestr("__MACOSX/._paper_1.html", "junk")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    test_dir = extract_state_zip(zip_path, workspace)
    assert test_dir == workspace / "papers"
    assert (test_dir / "paper_1.html").is_file()
    assert not (workspace / "__MACOSX").exists()


def test_extract_state_zip_rejects_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../escape.txt", "nope")
    with pytest.raises(McpExecutionError, match="mcp_state_archive_unsafe"):
        extract_state_zip(zip_path, tmp_path / "ws")


async def test_run_verify_script_pass_fail(tmp_path: Path) -> None:
    passing = tmp_path / "pass.py"
    passing.write_text("import sys; sys.exit(0)\n")
    outcome = await run_verify_script(passing, test_dir=tmp_path)
    assert outcome.passed and outcome.returncode == 0 and not outcome.timed_out

    failing = tmp_path / "fail.py"
    failing.write_text("import sys; print('nope'); sys.exit(1)\n")
    outcome = await run_verify_script(failing, test_dir=tmp_path)
    assert not outcome.passed and outcome.returncode == 1
    assert "nope" in outcome.stdout


async def test_run_verify_script_env_and_timeout(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        "sys.exit(0 if os.environ.get('FILESYSTEM_TEST_DIR', '').endswith('ws') else 1)\n"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await run_verify_script(probe, test_dir=workspace)
    assert outcome.passed

    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time; time.sleep(60)\n")
    outcome = await run_verify_script(sleeper, test_dir=tmp_path, timeout_seconds=1)
    assert not outcome.passed and outcome.timed_out


# ---------------------------------------------------------------------------
# End-to-end episode with an in-memory MCP server pair
# ---------------------------------------------------------------------------


def memory_filesystem_factory(workspace: Path) -> Client:
    """In-memory stand-in for the stdio filesystem server."""
    server = MCPServer("fake-filesystem", version="0.1.0")

    @server.tool()
    def write_file(path: str, content: str) -> str:
        """Write content to a file inside the workspace."""
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path}"

    @server.tool()
    def read_file(path: str) -> str:
        """Read a file from the workspace."""
        return (workspace / path).read_text()

    return Client(server)


def scripted_transport(replies: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        reply = replies[0] if len(replies) == 1 else replies.pop(0)
        return httpx.Response(200, json={"answer": reply})

    return httpx.MockTransport(handler)


def episode_settings(tmp_path: Path, repo: Path) -> Settings:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/episode.db",
        data_dir=tmp_path,
    )
    # MCPMARK_REPO_PATH is an unprefixed alias, so kwargs cannot populate it.
    settings.mcpmark_repo_path = repo
    return settings


async def run_episode_with_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[str],
    *,
    with_state: bool = True,
):
    repo = tmp_path / "repo"
    state_url = None
    if with_state:
        zip_path = make_state_zip(tmp_path, wrapped_dir=None)
        state_url = f"file://{zip_path}"
    make_task_dir(repo, state_url=state_url)
    config = make_config("http://agent.test/run")
    transport = scripted_transport(list(replies))

    import agent_eval.worker.mcp_executor as executor_module

    # The episode builds its own sync httpx.Client; swap in the mock transport.
    real_client = httpx.Client
    monkeypatch.setattr(
        executor_module.httpx,
        "Client",
        lambda *args, **kwargs: real_client(transport=transport, follow_redirects=False),
    )
    return await run_mcp_episode(
        config=config,
        task_ref="mcp:filesystem:easy/file_context/task_one",
        trial_index=0,
        timeout_seconds=60,
        settings=episode_settings(tmp_path, repo),
        client_factory=memory_filesystem_factory,
    )


async def test_episode_success_full_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        fenced({"action": "tool", "name": "read_file", "arguments": {"path": "note.txt"}}),
        fenced(
            {
                "action": "tool",
                "name": "write_file",
                "arguments": {"path": "answer.txt", "content": "done"},
            }
        ),
        fenced({"action": "stop", "answer": "wrote answer.txt"}),
    ]
    result = await run_episode_with_agent(tmp_path, monkeypatch, replies)
    assert result.success and result.error_type is None
    assert result.task_reward == 1
    trajectory = result.trajectory
    assert trajectory is not None
    assert trajectory["termination_reason"] == "stop"
    assert trajectory["verify"]["passed"] is True
    roles = [message["role"] for message in trajectory["messages"]]
    assert roles == ["agent", "tool", "agent", "tool", "agent"]
    tool_names = [
        message["name"] for message in trajectory["messages"] if message["role"] == "tool"
    ]
    assert tool_names == ["read_file", "write_file"]
    # the extracted state file was visible to the in-memory filesystem server
    read_result = trajectory["messages"][1]
    assert read_result["result"] == "hello"


async def test_episode_agent_never_writes_fails_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [fenced({"action": "stop", "answer": "gave up"})]
    result = await run_episode_with_agent(tmp_path, monkeypatch, replies)
    assert result.success
    assert result.task_reward == 0
    assert result.trajectory is not None
    assert result.trajectory["verify"]["passed"] is False


async def test_episode_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = ["garbage without a fence"]
    result = await run_episode_with_agent(tmp_path, monkeypatch, replies)
    assert result.success  # episode ran; agent broke the protocol
    assert result.error_type == "agent_protocol_error"
    assert result.task_reward == 0
    assert result.trajectory is not None
    assert result.trajectory["termination_reason"] == "agent_error"


async def test_episode_missing_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(McpExecutionError, match="mcp_task_missing"):
        await run_mcp_episode(
            config=make_config("http://agent.test/run"),
            task_ref="mcp:filesystem:easy/file_context/ghost",
            trial_index=0,
            timeout_seconds=60,
            settings=episode_settings(tmp_path, repo),
            client_factory=memory_filesystem_factory,
        )


async def test_episode_requires_repo_path(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/episode.db", data_dir=tmp_path
    )
    with pytest.raises(McpExecutionError, match="mcp_repo_path_missing"):
        await run_mcp_episode(
            config=make_config("http://agent.test/run"),
            task_ref="mcp:filesystem:easy/file_context/task_one",
            trial_index=0,
            timeout_seconds=60,
            settings=settings,
            client_factory=memory_filesystem_factory,
        )


# ---------------------------------------------------------------------------
# Run creation through runs/service.prepare_evaluation_run (locked profile)
# ---------------------------------------------------------------------------


async def mcp_run_fixture(tmp_path: Path, *, tasks: int = 10):
    repo = make_fake_repo(tmp_path, tasks=tasks)
    database_path = tmp_path / "mcp-run.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    async with session_scope(session_factory) as session:
        await import_mcpmark_filesystem(session, repo_dir=repo, suite="easy")
        agent = Agent(name="MCP Agent", provider="Provider", status=AgentStatus.ENABLED)
        session.add(agent)
        await session.flush()
        version = AgentVersion(
            agent_id=agent.id,
            version=1,
            endpoint_url="https://agent.example/mcp",
            auth_type=AuthType.NONE,
            request_template='{"query":"{{prompt}}"}',
            answer_jsonpath="$.answer",
        )
        judge = JudgeConfig(
            provider="deepseek", model="deepseek-chat", prompt_version="v1", enabled=True
        )
        session.add_all([version, judge])
        await session.flush()
        dataset_version_ids = [
            item.id for item in await session.scalars(select(DatasetVersion))
        ]
        ids = (version.id, judge.id, dataset_version_ids)
    return session_factory, engine, ids


async def test_mcp_filesystem_run_creation(tmp_path: Path) -> None:
    session_factory, engine, (version_id, judge_id, dataset_version_ids) = (
        await mcp_run_fixture(tmp_path)
    )
    try:
        async with session_scope(session_factory) as session:
            prepared = await prepare_evaluation_run(
                session,
                name="MCP filesystem easy",
                agent_version_ids=[version_id],
                dataset_version_ids=dataset_version_ids,
                judge_config_id=judge_id,
                score_config_id=None,
                profile_key=MCP_FILESYSTEM_PROFILE_KEY,
                run_scope=RunScope.SMOKE,
                target_ability=None,
                ranking_eligible=False,
            )
        assert prepared.question_count == 10
        assert prepared.run.ranking_eligible is False
    finally:
        await engine.dispose()


async def test_mcp_filesystem_run_rejects_incomplete_dataset(tmp_path: Path) -> None:
    # Only 9 of the 10 easy tasks imported: the locked profile stays hidden and
    # run creation must fail instead of silently running a partial suite.
    session_factory, engine, (version_id, judge_id, dataset_version_ids) = (
        await mcp_run_fixture(tmp_path, tasks=9)
    )
    try:
        async with session_scope(session_factory) as session:
            with pytest.raises(RunConfigurationError) as excinfo:
                await prepare_evaluation_run(
                    session,
                    name="MCP filesystem partial",
                    agent_version_ids=[version_id],
                    dataset_version_ids=dataset_version_ids,
                    judge_config_id=judge_id,
                    score_config_id=None,
                    profile_key=MCP_FILESYSTEM_PROFILE_KEY,
                    run_scope=RunScope.SMOKE,
                    target_ability=None,
                    ranking_eligible=False,
                )
            assert excinfo.value.code == "mcp_filesystem_incomplete"
    finally:
        await engine.dispose()


async def test_mcp_filesystem_run_rejects_ranking_eligible(tmp_path: Path) -> None:
    session_factory, engine, (version_id, judge_id, dataset_version_ids) = (
        await mcp_run_fixture(tmp_path)
    )
    try:
        async with session_scope(session_factory) as session:
            with pytest.raises(RunConfigurationError) as excinfo:
                await prepare_evaluation_run(
                    session,
                    name="MCP filesystem ranked",
                    agent_version_ids=[version_id],
                    dataset_version_ids=dataset_version_ids,
                    judge_config_id=judge_id,
                    score_config_id=None,
                    profile_key=MCP_FILESYSTEM_PROFILE_KEY,
                    run_scope=RunScope.SMOKE,
                    target_ability=None,
                    ranking_eligible=True,
                )
            assert excinfo.value.code == "mcp_filesystem_incomplete"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# ExceptionGroup unwrapping in execute_mcp_items
# ---------------------------------------------------------------------------


class _FakeResultWriter:
    def __init__(self) -> None:
        self.completions: list[AttemptCompletion] = []

    async def start_attempt(self, item_id: object, attempt_number: int) -> object:
        return uuid4()

    async def finish_attempt(self, completion: AttemptCompletion) -> None:
        self.completions.append(completion)

    async def finalize_item(self, **kwargs: object) -> None:
        return None


async def test_execute_mcp_items_unwraps_exception_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_eval.worker import mcp_executor

    async def boom(**kwargs: object) -> None:
        raise ExceptionGroup("episode", [RuntimeError("server crashed")])

    monkeypatch.setattr(mcp_executor, "run_mcp_episode", boom)

    async def record_outcome(outcome: object, prompt_characters: int) -> None:
        return None

    writer = _FakeResultWriter()
    item = McpWorkItem(
        item_id=uuid4(),
        run_agent_id=uuid4(),
        question_id=uuid4(),
        attempt_count=0,
        trial_index=0,
        task_ref="mcp:filesystem:easy/file_context/task_one",
        prompt_characters=10,
    )
    await execute_mcp_items(
        [item],
        agent_id=uuid4(),
        version_id=uuid4(),
        concurrency=1,
        config=make_config("http://agent.test/run"),
        settings=Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/x.db", data_dir=tmp_path
        ),
        writer=writer,  # type: ignore[arg-type]
        record_outcome=record_outcome,
    )
    assert len(writer.completions) == 1
    assert writer.completions[0].error_type == "mcp_episode_RuntimeError"
