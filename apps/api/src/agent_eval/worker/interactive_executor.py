"""Interactive (tau2-bench) execution for INTERACTIVE execution items.

Runs tau2 simulations for the platform's dumb HTTP agents. tau2 orchestration
is synchronous and CPU/IO mixed, so episodes run on a bounded
``ThreadPoolExecutor``; each episode is wrapped in the worker's resource
limiters (agent request slots plus the DeepSeek/simulator slot).

tau2 is imported lazily inside the worker process only: ``TAU2_DATA_DIR`` and
``DEEPSEEK_API_KEY`` must be set before the first tau2 import, and the NL
assertions evaluator's default OpenAI model is replaced with DeepSeek at setup
time so no implicit OpenAI dependency remains.
"""

import asyncio
import importlib.metadata
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx

from agent_eval.agents.inference import (
    AgentPromptError,
    AttemptCompletion,
    InferenceConfig,
    InferenceOutcome,
    elapsed_ms,
)
from agent_eval.core.settings import Settings
from agent_eval.db.enums import AttemptStatus
from agent_eval.worker.limits import (
    RequestLimit,
    agent_request_slots,
    concurrency_slot,
)
from agent_eval.worker.result_writer import ResultWriter

SIMULATOR_MODEL = "deepseek/deepseek-chat"
NL_ASSERTIONS_MODEL = "deepseek/deepseek-chat"
TASK_SPLIT = "base"
MAX_STEPS = 100
MAX_ERRORS = 10
TASK_REF_PREFIX = "tau2"

_setup_lock = threading.Lock()
_setup_done = False
_tasks_cache: dict[str, dict[str, Any]] = {}


class InteractiveExecutionError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


@dataclass(frozen=True)
class InteractiveWorkItem:
    item_id: UUID
    run_agent_id: UUID
    question_id: UUID
    attempt_count: int
    trial_index: int
    task_ref: str
    prompt_characters: int


@dataclass(frozen=True)
class EpisodeResult:
    success: bool
    error_type: str | None
    task_reward: Decimal | None
    answer: str | None
    trajectory: dict[str, Any] | None
    latency_ms: int
    # Tested-agent token totals (feed the standard attempt/cost aggregation);
    # None when the agent endpoint reported no usage.
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    # DeepSeek user-simulator tokens are evaluation overhead, not agent cost;
    # they are stored per item and surface in run metadata only.
    simulator_input_tokens: int | None = None
    simulator_output_tokens: int | None = None


def tau2_version() -> str:
    try:
        return importlib.metadata.version("tau2")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def parse_task_ref(task_ref: str) -> tuple[str, str]:
    parts = task_ref.split(":", 2)
    if len(parts) != 3 or parts[0] != TASK_REF_PREFIX or not parts[1] or not parts[2]:
        raise InteractiveExecutionError("interactive_task_ref_invalid")
    return parts[1], parts[2]


def ensure_tau2_runtime(settings: Settings) -> None:
    """Set up process-global tau2 state exactly once (env vars + LLM defaults)."""
    global _setup_done
    with _setup_lock:
        if _setup_done:
            return
        if settings.tau2_data_dir is None:
            raise InteractiveExecutionError("interactive_data_dir_missing")
        if settings.deepseek_api_key is None:
            raise InteractiveExecutionError("interactive_simulator_key_missing")
        os.environ["TAU2_DATA_DIR"] = str(settings.tau2_data_dir)
        os.environ["DEEPSEEK_API_KEY"] = settings.deepseek_api_key.get_secret_value()
        # Importing tau2 resolves the data directory; patch the NL assertions
        # evaluator immediately so it never falls back to the OpenAI default.
        import tau2.evaluator.evaluator_nl_assertions as nl_assertions

        nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = NL_ASSERTIONS_MODEL
        nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {"temperature": 0.0}
        _setup_done = True


def _load_tasks(domain: str) -> dict[str, Any]:
    with _setup_lock:
        cached = _tasks_cache.get(domain)
        if cached is not None:
            return cached
        from tau2.registry import registry

        tasks = registry.get_tasks_loader(domain)(TASK_SPLIT)
        by_id = {task.id: task for task in tasks}
        _tasks_cache[domain] = by_id
        return by_id


def run_tau2_episode(
    *,
    config: InferenceConfig,
    task_ref: str,
    trial_index: int,
    timeout_seconds: int,
    settings: Settings,
) -> EpisodeResult:
    """Run one tau2 simulation synchronously; call from a worker thread."""
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.registry import registry
    from tau2.runner.simulation import run_simulation
    from tau2.user.user_simulator import UserSimulator

    from agent_eval.agents.interactive import (
        AGENT_PROTOCOL,
        AgentProtocolError,
        HttpTextProtocolAgent,
    )

    started_at = perf_counter()
    domain, task_id = parse_task_ref(task_ref)
    ensure_tau2_runtime(settings)
    task = _load_tasks(domain).get(task_id)
    if task is None:
        raise InteractiveExecutionError("interactive_task_missing")
    environment = registry.get_env_constructor(domain)()
    base_trajectory: dict[str, Any] = {
        "tau2_version": tau2_version(),
        "domain": domain,
        "task_id": task_id,
        "task_split": TASK_SPLIT,
        "trial_index": trial_index,
        "agent_protocol": AGENT_PROTOCOL,
        "simulator_model": SIMULATOR_MODEL,
    }
    with httpx.Client(follow_redirects=False) as client:
        agent = HttpTextProtocolAgent(
            tools=environment.get_tools(),
            domain_policy=environment.get_policy(),
            client=client,
            config=config,
        )
        try:
            user_tools = environment.get_user_tools()
        except Exception:  # retail and other domains without user tools
            user_tools = None
        user = UserSimulator(
            llm=SIMULATOR_MODEL,
            instructions=str(task.user_scenario),
            tools=user_tools or None,
            llm_args={"temperature": 0.0},
        )
        orchestrator = Orchestrator(
            domain=domain,
            agent=agent,
            user=user,
            environment=environment,
            task=task,
            max_steps=MAX_STEPS,
            max_errors=MAX_ERRORS,
            timeout=float(timeout_seconds),
        )
        try:
            simulation = run_simulation(orchestrator)
        except AgentProtocolError as error:
            latency_ms = elapsed_ms(started_at)
            trajectory = {
                **base_trajectory,
                "turns": len(orchestrator.trajectory),
                "termination_reason": "agent_error",
                "agent_protocol_error": str(error),
                "reward_info": None,
                "messages": _dump_messages(orchestrator.trajectory),
            }
            # the failed turn produced no message, so read usage the agent
            # already spent on HTTP calls (including the rejected replies)
            spent = agent.session_usage
            input_tokens = spent["prompt_tokens"] if spent else None
            output_tokens = spent["completion_tokens"] if spent else None
            simulator_input, simulator_output = _sum_message_usage(
                orchestrator.trajectory, role="user"
            )
            return EpisodeResult(
                success=True,
                error_type="agent_protocol_error",
                task_reward=Decimal(0),
                answer=f"agent_protocol_error: {error}",
                trajectory=trajectory,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=_total_tokens(input_tokens, output_tokens),
                simulator_input_tokens=simulator_input,
                simulator_output_tokens=simulator_output,
            )

    latency_ms = elapsed_ms(started_at)
    reward_info = simulation.reward_info
    reward = float(reward_info.reward) if reward_info is not None else 0.0
    input_tokens, output_tokens = _sum_message_usage(simulation.messages, role="assistant")
    simulator_input, simulator_output = _sum_message_usage(simulation.messages, role="user")
    termination = simulation.termination_reason
    termination_value = getattr(termination, "value", str(termination))
    trajectory = {
        **base_trajectory,
        "turns": len(simulation.messages),
        "termination_reason": termination_value,
        "agent_protocol_error": None,
        "reward_info": reward_info.model_dump(mode="json") if reward_info is not None else None,
        "messages": _dump_messages(simulation.messages),
    }
    answer = (
        f"reward={reward:.4f}; termination={termination_value}; "
        f"turns={len(simulation.messages)}; {_reward_summary(reward_info)}"
    )
    return EpisodeResult(
        success=True,
        error_type=None,
        task_reward=Decimal(str(round(reward, 4))),
        answer=answer,
        trajectory=trajectory,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=_total_tokens(input_tokens, output_tokens),
        simulator_input_tokens=simulator_input,
        simulator_output_tokens=simulator_output,
    )


def _dump_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [message.model_dump(mode="json") for message in messages]


def _total_tokens(input_tokens: int | None, output_tokens: int | None) -> int | None:
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


def _sum_message_usage(
    messages: list[Any], *, role: str
) -> tuple[int | None, int | None]:
    """Sum prompt/completion tokens over messages of one role (tau2 usage shape)."""
    prompt_tokens = 0
    completion_tokens = 0
    observed = False
    for message in messages:
        if getattr(message, "role", None) != role:
            continue
        usage = getattr(message, "usage", None)
        if not usage:
            continue
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            prompt_tokens += prompt
            completion_tokens += completion
            observed = True
    if not observed:
        return None, None
    return prompt_tokens, completion_tokens


def _reward_summary(reward_info: Any) -> str:
    if reward_info is None:
        return "no reward info"
    parts: list[str] = []
    db_check = getattr(reward_info, "db_check", None)
    if db_check is not None:
        parts.append(f"db={getattr(db_check, 'db_reward', '?')}")
    action_checks = getattr(reward_info, "action_checks", None) or []
    if action_checks:
        passed = sum(1 for check in action_checks if getattr(check, "action_match", False))
        parts.append(f"actions={passed}/{len(action_checks)}")
    nl_assertions = getattr(reward_info, "nl_assertions", None) or []
    if nl_assertions:
        met = sum(1 for check in nl_assertions if getattr(check, "met", False))
        parts.append(f"nl={met}/{len(nl_assertions)}")
    communicate_checks = getattr(reward_info, "communicate_checks", None) or []
    if communicate_checks:
        met = sum(1 for check in communicate_checks if getattr(check, "met", False))
        parts.append(f"communicate={met}/{len(communicate_checks)}")
    return "; ".join(parts) if parts else "reward components empty"


async def execute_interactive_items(
    items: list[InteractiveWorkItem],
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
    simulator_request_limit: RequestLimit | None = None,
) -> None:
    if not items:
        return
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(
        max_workers=min(settings.interactive_concurrency, len(items)),
        thread_name_prefix="tau2-episode",
    )
    try:
        await asyncio.gather(
            *[
                _run_interactive_item(
                    item,
                    agent_id=agent_id,
                    version_id=version_id,
                    concurrency=concurrency,
                    config=config,
                    settings=settings,
                    writer=writer,
                    pool=pool,
                    loop=loop,
                    record_outcome=record_outcome,
                    request_limit=request_limit,
                    version_request_limit=version_request_limit,
                    simulator_request_limit=simulator_request_limit,
                )
                for item in items
            ]
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


async def _run_interactive_item(
    item: InteractiveWorkItem,
    *,
    agent_id: UUID,
    version_id: UUID,
    concurrency: int,
    config: InferenceConfig,
    settings: Settings,
    writer: ResultWriter,
    pool: ThreadPoolExecutor,
    loop: asyncio.AbstractEventLoop,
    record_outcome: Callable[[InferenceOutcome, int], Awaitable[None]],
    request_limit: RequestLimit | None,
    version_request_limit: RequestLimit | None,
    simulator_request_limit: RequestLimit | None,
) -> None:
    attempt_id = await writer.start_attempt(item.item_id, item.attempt_count + 1)
    if attempt_id is None:
        return
    started_at = perf_counter()
    try:
        async with (
            agent_request_slots(
                version_request_limit,
                request_limit,
                version_id=str(version_id),
                version_limit=concurrency,
            ),
            concurrency_slot(simulator_request_limit),
        ):
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    pool,
                    lambda: run_tau2_episode(
                        config=config,
                        task_ref=item.task_ref,
                        trial_index=item.trial_index,
                        timeout_seconds=settings.interactive_timeout_seconds,
                        settings=settings,
                    ),
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
    except (TimeoutError, InteractiveExecutionError) as error:
        error_type = (
            "agent_timeout"
            if isinstance(error, TimeoutError)
            else error.error_type
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
        result = EpisodeResult(
            success=False,
            error_type=f"interactive_episode_{type(error).__name__}"[:80],
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
        simulator_input_tokens=result.simulator_input_tokens,
        simulator_output_tokens=result.simulator_output_tokens,
    )
    await record_outcome(outcome, item.prompt_characters)
