"""MCP (MCPMark filesystem) execution for INTERACTIVE execution items.

Runs MCPMark filesystem tasks against the platform's dumb HTTP agents: the
task's initial state zip is downloaded (and cached) into a per-episode
temporary workspace, a stdio filesystem MCP server is started with that
workspace as its only allowed directory, and the agent drives it through the
shared fenced-JSON text protocol (``agents/protocol.py``). When the agent
stops (or the turn budget is exhausted), the task's ``verify.py`` decides the
binary reward.

Unlike tau2 orchestration the MCP client is asyncio-native, so episodes run
as bounded asyncio tasks instead of a ThreadPoolExecutor; agent HTTP calls
stay synchronous and hop through ``asyncio.to_thread``.

mcp is imported lazily inside episode setup only: the API process and workers
without the ``mcp`` dependency group must stay importable without it.

TRUST BOUNDARY: ``verify.py`` and the task bundle are external code from the
pinned MCPMark checkout (github.com/eval-sys/mcpmark, Apache-2.0). They run as
the worker OS user with the worker's environment, so the checkout must come
from a commit the operator pinned deliberately; the executor never fetches
task code from the network (only the initial-state zip from
``meta_data.stateUrl``).
"""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import sys
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx

from agent_eval.agents.inference import (
    AgentPromptError,
    AgentPromptResult,
    AttemptCompletion,
    InferenceConfig,
    InferenceOutcome,
    elapsed_ms,
    post_agent_prompt,
)
from agent_eval.agents.protocol import (
    AgentProtocolError,
    ProtocolParseError,
    build_correction_message,
    merge_token_usage,
    parse_mcp_action,
    render_mcp_prompt,
)
from agent_eval.core.settings import Settings
from agent_eval.core.tracing import (
    SpanAttributes,
    endpoint_host,
    set_attributes,
    trace_span,
)
from agent_eval.db.enums import AttemptStatus
from agent_eval.worker.interactive_executor import EpisodeResult
from agent_eval.worker.limits import RequestLimit, agent_request_slots
from agent_eval.worker.result_writer import ResultWriter

FILESYSTEM_SERVER_PACKAGE = "@modelcontextprotocol/server-filesystem@2025.12.18"
TASK_REF_PREFIX = "mcp"
FILESYSTEM_DOMAIN = "filesystem"
MCP_AGENT_PROTOCOL = "http-text-json-v1"
MAX_TURNS = 40
VERIFY_TIMEOUT_SECONDS = 120
STATE_DOWNLOAD_TIMEOUT_SECONDS = 120
MAX_LOG_CHARS = 4000
MAX_STATE_BYTES = 512 * 1024 * 1024


class McpExecutionError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class McpClientLike(Protocol):
    async def list_tools(self) -> Any: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


McpClientFactory = Callable[[Path], AbstractAsyncContextManager[McpClientLike]]


@dataclass(frozen=True)
class McpWorkItem:
    item_id: UUID
    run_agent_id: UUID
    question_id: UUID
    attempt_count: int
    trial_index: int
    task_ref: str
    prompt_characters: int


@dataclass(frozen=True)
class VerifyOutcome:
    passed: bool
    returncode: int | None
    timed_out: bool
    stdout: str
    stderr: str


def mcp_version() -> str:
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def is_mcp_task_ref(task_ref: str) -> bool:
    return task_ref.startswith(f"{TASK_REF_PREFIX}:")


def parse_mcp_task_ref(task_ref: str) -> tuple[str, str, str]:
    """Split ``mcp:filesystem:<suite>/<category>/<task_id>`` into its parts."""
    parts = task_ref.split(":", 2)
    if len(parts) != 3 or parts[0] != TASK_REF_PREFIX or parts[1] != FILESYSTEM_DOMAIN:
        raise McpExecutionError("mcp_task_ref_invalid")
    segments = parts[2].split("/")
    if len(segments) != 3 or any(not segment for segment in segments):
        raise McpExecutionError("mcp_task_ref_invalid")
    return segments[0], segments[1], segments[2]


def resolve_task_dir(settings: Settings, task_ref: str) -> Path:
    suite, category, task_id = parse_mcp_task_ref(task_ref)
    if settings.mcpmark_repo_path is None:
        raise McpExecutionError("mcp_repo_path_missing")
    task_dir = (
        settings.mcpmark_repo_path
        / "tasks"
        / "filesystem"
        / suite
        / category
        / task_id
    )
    if not (task_dir / "meta.json").is_file() or not (task_dir / "verify.py").is_file():
        raise McpExecutionError("mcp_task_missing")
    return task_dir


def _expected_integrity(meta_data: dict[str, Any]) -> tuple[str | None, int | None]:
    """Optional sha256/size pins for the state zip (MCPMark may add them)."""
    sha256 = meta_data.get("stateSha256") or meta_data.get("sha256")
    size = meta_data.get("stateSize") or meta_data.get("size")
    return (
        sha256 if isinstance(sha256, str) and len(sha256) == 64 else None,
        size if isinstance(size, int) and not isinstance(size, bool) else None,
    )


async def ensure_state_zip(
    state_url: str, *, task_dir: Path, cache_dir: Path
) -> Path:
    """Return a local copy of the task's initial-state zip, download-cached.

    ``file://`` URLs and paths relative to the task directory are supported so
    offline runs and tests never hit the network.
    """
    if state_url.startswith("file://"):
        local = Path(urlparse(state_url).path)
        if not local.is_file():
            raise McpExecutionError("mcp_state_missing")
        return local
    if "://" not in state_url:
        local = task_dir / state_url
        if not local.is_file():
            raise McpExecutionError("mcp_state_missing")
        return local
    key = hashlib.sha256(state_url.encode()).hexdigest()[:16]
    name = Path(urlparse(state_url).path).name or "state.zip"
    target = cache_dir / key / name
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(".download")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=STATE_DOWNLOAD_TIMEOUT_SECONDS
        ) as client, client.stream("GET", state_url) as response:
            if response.status_code != 200:
                raise McpExecutionError("mcp_state_download_failed")
            with tmp_target.open("wb") as stream:
                async for chunk in response.aiter_bytes(1024 * 256):
                    stream.write(chunk)
                    if stream.tell() > MAX_STATE_BYTES:
                        raise McpExecutionError("mcp_state_too_large")
        tmp_target.replace(target)
    except httpx.HTTPError as error:
        tmp_target.unlink(missing_ok=True)
        raise McpExecutionError("mcp_state_download_failed") from error
    except Exception:
        tmp_target.unlink(missing_ok=True)
        raise
    return target


def verify_state_integrity(zip_path: Path, meta_data: dict[str, Any]) -> None:
    expected_sha, expected_size = _expected_integrity(meta_data)
    content = zip_path.read_bytes()
    if expected_size is not None and len(content) != expected_size:
        raise McpExecutionError("mcp_state_integrity_mismatch")
    if expected_sha is not None and hashlib.sha256(content).hexdigest() != expected_sha:
        raise McpExecutionError("mcp_state_integrity_mismatch")


def extract_state_zip(zip_path: Path, workspace: Path) -> Path:
    """Extract the state zip into the workspace; returns the test root.

    MCPMark zips wrap the files in a single top-level directory (the category
    name); in that case the wrapped directory is the FILESYSTEM_TEST_DIR.
    macOS metadata junk (``__MACOSX``) is discarded before unwrapping.
    """
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            normalized = Path(member)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise McpExecutionError("mcp_state_archive_unsafe")
        archive.extractall(workspace)
    for junk in workspace.glob("__MACOSX"):
        shutil.rmtree(junk, ignore_errors=True)
    entries = list(workspace.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return workspace


async def run_verify_script(
    verify_path: Path, *, test_dir: Path, timeout_seconds: int = VERIFY_TIMEOUT_SECONDS
) -> VerifyOutcome:
    """Run the task's verify.py; exit code 0 means every check passed.

    verify.py is external MCPMark code (see the module docstring for the trust
    boundary). It runs in its own process group so a timeout kills the whole
    group; stdout/stderr are captured capped for the trajectory.
    """
    env = dict(os.environ)
    env["FILESYSTEM_TEST_DIR"] = str(test_dir)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(verify_path),
        cwd=verify_path.parent,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        stdout, stderr = await process.communicate()
    return VerifyOutcome(
        passed=not timed_out and process.returncode == 0,
        returncode=process.returncode,
        timed_out=timed_out,
        stdout=stdout.decode("utf-8", errors="replace")[-MAX_LOG_CHARS:],
        stderr=stderr.decode("utf-8", errors="replace")[-MAX_LOG_CHARS:],
    )


def _stdio_filesystem_client(workspace: Path) -> AbstractAsyncContextManager[McpClientLike]:
    """Default client factory: the pinned npm filesystem server over stdio.

    The mcp SDK only inherits a safe environment subset for stdio children, so
    proxy variables are forwarded explicitly: npx needs them when the package
    cache is cold. The stdio transport itself never touches the network.
    """
    from mcp import Client, StdioServerParameters

    proxy_env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    }
    params = StdioServerParameters(
        command="npx",
        args=["-y", FILESYSTEM_SERVER_PACKAGE, str(workspace)],
        env=proxy_env,
        # The npm server resolves relative tool paths against its cwd; pin it
        # inside the workspace so "." stays within the allowed directory.
        cwd=str(workspace),
    )
    return Client(params)


def _tool_schemas(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


def _tool_result_text(result: Any) -> str:
    parts = [
        content.text
        for content in getattr(result, "content", [])
        if getattr(content, "type", None) == "text"
    ]
    return "\n".join(parts)


async def run_mcp_episode(
    *,
    config: InferenceConfig,
    task_ref: str,
    trial_index: int,
    timeout_seconds: int,
    settings: Settings,
    trace_attributes: SpanAttributes | None = None,
    client_factory: McpClientFactory | None = None,
) -> EpisodeResult:
    """Run one MCPMark filesystem episode; call from the worker event loop."""
    with trace_span(
        f"mcp.episode {task_ref} trial={trial_index}",
        attributes={
            "gen_ai.operation.name": "mcp_episode",
            "agent_eval.task_ref": task_ref,
            "agent_eval.trial_index": trial_index,
            **(trace_attributes or {}),
        },
    ) as episode_span:
        return await _run_mcp_episode(
            episode_span=episode_span,
            config=config,
            task_ref=task_ref,
            trial_index=trial_index,
            timeout_seconds=timeout_seconds,
            settings=settings,
            client_factory=client_factory,
        )


async def _run_mcp_episode(
    *,
    episode_span: Any,
    config: InferenceConfig,
    task_ref: str,
    trial_index: int,
    timeout_seconds: int,
    settings: Settings,
    client_factory: McpClientFactory | None,
) -> EpisodeResult:
    started_at = perf_counter()
    suite, category, task_id = parse_mcp_task_ref(task_ref)
    task_dir = resolve_task_dir(settings, task_ref)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    meta_data = meta.get("meta_data") if isinstance(meta, dict) else None
    if not isinstance(meta_data, dict):
        raise McpExecutionError("mcp_task_meta_invalid")
    description = (task_dir / "description.md").read_text(encoding="utf-8")
    base_trajectory: dict[str, Any] = {
        "mcp_version": mcp_version(),
        "suite": suite,
        "category": category,
        "task_id": task_id,
        "trial_index": trial_index,
        "agent_protocol": MCP_AGENT_PROTOCOL,
        "server": FILESYSTEM_SERVER_PACKAGE if client_factory is None else "injected",
    }
    workspace = Path(tempfile.mkdtemp(prefix="mcp-episode-"))
    spent: list[AgentPromptResult] = []
    messages: list[dict[str, Any]] = []
    termination_reason = "error"
    final_answer: str | None = None
    protocol_error: str | None = None
    verify: VerifyOutcome | None = None
    try:
        state_url = meta_data.get("stateUrl")
        test_dir = workspace
        if isinstance(state_url, str) and state_url:
            zip_path = await ensure_state_zip(
                state_url,
                task_dir=task_dir,
                cache_dir=settings.data_dir / "mcpmark-states",
            )
            verify_state_integrity(zip_path, meta_data)
            test_dir = extract_state_zip(zip_path, workspace)
        async with (client_factory or _stdio_filesystem_client)(test_dir) as client:
            try:
                loop_result = await asyncio.wait_for(
                    _protocol_loop(
                        client,
                        config=config,
                        description=description,
                        workspace=test_dir,
                        timeout_seconds=timeout_seconds,
                        spent=spent,
                        messages=messages,
                    ),
                    timeout=timeout_seconds + 30,
                )
                termination_reason = loop_result[0]
                final_answer = loop_result[1]
            except AgentProtocolError as error:
                termination_reason = "agent_error"
                protocol_error = str(error)
        # verify.py runs while the workspace still exists; it is external
        # MCPMark code (see the module docstring for the trust boundary).
        verify = await run_verify_script(task_dir / "verify.py", test_dir=test_dir)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    latency_ms = elapsed_ms(started_at)
    reward = 1.0 if verify is not None and verify.passed else 0.0
    usage = merge_token_usage(spent)
    input_tokens = usage["prompt_tokens"] if usage else None
    output_tokens = usage["completion_tokens"] if usage else None
    trajectory = {
        **base_trajectory,
        "turns": len([message for message in messages if message["role"] == "agent"]),
        "termination_reason": termination_reason,
        "agent_protocol_error": protocol_error,
        "verify": {
            "passed": verify.passed,
            "returncode": verify.returncode,
            "timed_out": verify.timed_out,
            "stdout": verify.stdout,
            "stderr": verify.stderr,
        },
        "messages": messages,
    }
    answer = (
        f"reward={reward:.4f}; termination={termination_reason}; "
        f"turns={trajectory['turns']}; verify={'pass' if verify.passed else 'fail'}"
    )
    if final_answer:
        answer = f"{answer}; answer={final_answer[:200]}"
    set_attributes(
        episode_span,
        {
            "agent_eval.suite": suite,
            "agent_eval.category": category,
            "agent_eval.task_id": task_id,
            "agent_eval.agent_protocol": MCP_AGENT_PROTOCOL,
            "agent_eval.mcp_version": mcp_version(),
            "agent_eval.reward": reward,
            "agent_eval.turns": trajectory["turns"],
            "agent_eval.termination_reason": termination_reason,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
        },
    )
    return EpisodeResult(
        success=True,
        error_type="agent_protocol_error" if protocol_error is not None else None,
        task_reward=Decimal(str(round(reward, 4))),
        answer=answer,
        trajectory=trajectory,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
    )


async def _protocol_loop(
    client: McpClientLike,
    *,
    config: InferenceConfig,
    description: str,
    workspace: Path,
    timeout_seconds: int,
    spent: list[AgentPromptResult],
    messages: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """Drive the fenced-JSON tool loop; returns (termination_reason, answer).

    Raises AgentProtocolError when the agent fails to produce a valid action
    even after the one-shot correction, and TimeoutError at the turn budget of
    ``timeout_seconds``.
    """
    tools_result = await client.list_tools()
    tool_schemas = _tool_schemas(tools_result.tools)
    tool_names = {tool.name for tool in tools_result.tools}
    history: list[str] = []
    correction: str | None = None
    async with asyncio.timeout(timeout_seconds):
        with httpx.Client(follow_redirects=False) as http_client:
            for _turn in range(MAX_TURNS):
                reply = await _ask(
                    http_client, config,
                    description=description,
                    workspace=workspace,
                    tool_schemas=tool_schemas,
                    history=history,
                    correction=correction,
                    spent=spent,
                )
                try:
                    action = parse_mcp_action(reply.answer, tool_names=tool_names)
                except ProtocolParseError as first_error:
                    if correction is not None:
                        raise AgentProtocolError(str(first_error)) from first_error
                    correction = build_correction_message(first_error, reply.answer)
                    retry = await _ask(
                        http_client, config,
                        description=description,
                        workspace=workspace,
                        tool_schemas=tool_schemas,
                        history=history,
                        correction=correction,
                        spent=spent,
                    )
                    try:
                        action = parse_mcp_action(retry.answer, tool_names=tool_names)
                    except ProtocolParseError as second_error:
                        raise AgentProtocolError(str(second_error)) from second_error
                    reply = retry
                correction = None
                messages.append({"role": "agent", "content": reply.answer})
                if action.action == "stop":
                    return "stop", action.answer
                assert action.tool_name is not None and action.tool_arguments is not None
                history.append(
                    f"agent called tool {action.tool_name} with arguments "
                    f"{json.dumps(action.tool_arguments, ensure_ascii=False)}"
                )
                with trace_span(
                    f"tool.execute {action.tool_name}",
                    attributes={
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": action.tool_name,
                    },
                ) as tool_span:
                    result = await client.call_tool(action.tool_name, action.tool_arguments)
                    is_error = bool(getattr(result, "is_error", False))
                    set_attributes(tool_span, {"agent_eval.tool_error": is_error})
                result_text = _tool_result_text(result)
                messages.append(
                    {
                        "role": "tool",
                        "name": action.tool_name,
                        "arguments": action.tool_arguments,
                        "result": result_text,
                        "is_error": is_error,
                    }
                )
                history.append(f"tool result: {result_text[:20000]}")
            return "max_turns", None


async def _ask(
    http_client: httpx.Client,
    config: InferenceConfig,
    *,
    description: str,
    workspace: Path,
    tool_schemas: list[dict[str, Any]],
    history: list[str],
    correction: str | None,
    spent: list[AgentPromptResult],
) -> AgentPromptResult:
    prompt = render_mcp_prompt(
        task_description=description,
        tool_schemas=tool_schemas,
        history=history,
        workspace=str(workspace),
        correction=correction,
    )
    with trace_span(
        "gen_ai.chat agent",
        attributes={
            "gen_ai.system": endpoint_host(config.endpoint_url),
            "gen_ai.operation.name": "chat",
            "agent_eval.agent_protocol": MCP_AGENT_PROTOCOL,
        },
    ) as span:
        try:
            result = await asyncio.to_thread(post_agent_prompt, http_client, config, prompt)
        except AgentPromptError as error:
            set_attributes(span, {"error.type": error.error_type})
            raise
        spent.append(result)
        set_attributes(
            span,
            {
                "gen_ai.usage.input_tokens": result.input_tokens,
                "gen_ai.usage.output_tokens": result.output_tokens,
                "gen_ai.usage.total_tokens": result.total_tokens,
                "agent_eval.latency_ms": result.latency_ms,
                "http.status_code": result.status_code,
            },
        )
        return result


async def execute_mcp_items(
    items: list[McpWorkItem],
    *,
    agent_id: UUID,
    version_id: UUID,
    concurrency: int,
    config: InferenceConfig,
    settings: Settings,
    writer: ResultWriter,
    record_outcome: Callable[[InferenceOutcome, int], Awaitable[None]],
    request_limit: RequestLimit | None = None,
    version_request_limit: RequestLimit | None = None,
) -> None:
    if not items:
        return
    semaphore = asyncio.Semaphore(min(settings.interactive_concurrency, len(items)))
    await asyncio.gather(
        *[
            _run_mcp_item(
                item,
                agent_id=agent_id,
                version_id=version_id,
                concurrency=concurrency,
                config=config,
                settings=settings,
                writer=writer,
                semaphore=semaphore,
                record_outcome=record_outcome,
                request_limit=request_limit,
                version_request_limit=version_request_limit,
            )
            for item in items
        ]
    )


async def _run_mcp_item(
    item: McpWorkItem,
    *,
    agent_id: UUID,
    version_id: UUID,
    concurrency: int,
    config: InferenceConfig,
    settings: Settings,
    writer: ResultWriter,
    semaphore: asyncio.Semaphore,
    record_outcome: Callable[[InferenceOutcome, int], Awaitable[None]],
    request_limit: RequestLimit | None,
    version_request_limit: RequestLimit | None,
) -> None:
    attempt_id = await writer.start_attempt(item.item_id, item.attempt_count + 1)
    if attempt_id is None:
        return
    started_at = perf_counter()
    try:
        async with (
            semaphore,
            agent_request_slots(
                version_request_limit,
                request_limit,
                version_id=str(version_id),
                version_limit=concurrency,
            ),
        ):
            result = await asyncio.wait_for(
                run_mcp_episode(
                    config=config,
                    task_ref=item.task_ref,
                    trial_index=item.trial_index,
                    timeout_seconds=settings.interactive_timeout_seconds,
                    settings=settings,
                    trace_attributes={
                        "agent_eval.item_id": str(item.item_id),
                        "agent_eval.run_agent_id": str(item.run_agent_id),
                        "agent_eval.question_id": str(item.question_id),
                    },
                ),
                timeout=settings.interactive_timeout_seconds + 120,
            )
    except AgentPromptError as error:
        result = EpisodeResult(
            success=False,
            error_type=error.error_type,
            task_reward=None,
            answer=None,
            trajectory=None,
            latency_ms=elapsed_ms(started_at),
        )
    except (TimeoutError, McpExecutionError) as error:
        error_type = (
            "agent_timeout" if isinstance(error, TimeoutError) else error.error_type
        )
        result = EpisodeResult(
            success=False,
            error_type=error_type,
            task_reward=None,
            answer=None,
            trajectory=None,
            latency_ms=elapsed_ms(started_at),
        )
    except Exception as error:
        # anyio task groups surface failures as ExceptionGroup; unwrap the first
        # leaf so error_type stays a meaningful classification.
        if isinstance(error, ExceptionGroup) and error.exceptions:
            error = error.exceptions[0]
        result = EpisodeResult(
            success=False,
            error_type=f"mcp_episode_{type(error).__name__}"[:80],
            task_reward=None,
            answer=None,
            trajectory=None,
            latency_ms=elapsed_ms(started_at),
        )
    await writer.finish_attempt(
        AttemptCompletion(
            attempt_id=attempt_id,
            status=AttemptStatus.SUCCEEDED if result.success else AttemptStatus.FAILED,
            status_code=None,
            latency_ms=result.latency_ms,
            error_type=result.error_type,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )
    )
    outcome = InferenceOutcome(
        success=result.success,
        canceled=False,
        answer=result.answer,
        citations=[],
        response_content=None,
        latency_ms=result.latency_ms,
        attempt_count=item.attempt_count,
        error_type=result.error_type,
    )
    await writer.finalize_item(
        item_id=item.item_id,
        run_agent_id=item.run_agent_id,
        agent_id=agent_id,
        question_id=item.question_id,
        outcome=outcome,
        task_reward=result.task_reward,
        trajectory=result.trajectory,
    )
    await record_outcome(outcome, item.prompt_characters)
