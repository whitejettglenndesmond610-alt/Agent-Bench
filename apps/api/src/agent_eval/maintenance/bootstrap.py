import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.auth.encryption import TokenCipher
from agent_eval.core.settings import Settings
from agent_eval.datasets.importer import import_question_records
from agent_eval.datasets.schema import load_question_records, validate_demo_distribution
from agent_eval.db.enums import AgentStatus, AgentVersionLifecycle, AuthType, DatasetStatus
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    DatasetVersion,
    JudgeConfig,
    LeaderboardSnapshot,
    Question,
    ScoreConfig,
)
from agent_eval.scoring.configs import (
    BALANCED_V2_ABILITY_WEIGHTS,
    BALANCED_V2_SCORE_NAME,
    LEGACY_SCORE_NAME,
    ensure_balanced_v2_score_config,
    has_ability_weights,
)

DEFAULT_JUDGE = ("deepseek", "deepseek-chat", "v1")
DEFAULT_SCORE_NAME = LEGACY_SCORE_NAME
DEFAULT_SCORE_VALUES = {
    "reasoning_weight": 40,
    "retrieval_weight": 35,
    "data_weight": 25,
    "instruction_following_weight": 0,
    "long_context_weight": 0,
    "retrieval_answer_weight": 70,
    "retrieval_citation_weight": 30,
    "citation_authenticity_weight": 50,
    "citation_support_weight": 50,
}


@dataclass(frozen=True)
class BootstrapResult:
    question_count: int
    dataset_count: int
    judge_created: bool
    score_created: bool


async def bootstrap_demo(session: AsyncSession, dataset_path: Path) -> BootstrapResult:
    validate_dataset_manifest(dataset_path)
    records = load_question_records(dataset_path)
    validate_demo_distribution(records)
    datasets = await import_question_records(session, records)

    provider, model, prompt_version = DEFAULT_JUDGE
    judge = await session.scalar(
        select(JudgeConfig).where(
            JudgeConfig.provider == provider,
            JudgeConfig.model == model,
            JudgeConfig.prompt_version == prompt_version,
        )
    )
    judge_created = judge is None
    if judge is None:
        session.add(
            JudgeConfig(
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                enabled=True,
            )
        )
    elif not judge.enabled:
        raise RuntimeError("default judge exists but is disabled")

    score = await session.scalar(select(ScoreConfig).where(ScoreConfig.name == DEFAULT_SCORE_NAME))
    score_created = score is None
    if score is None:
        session.add(ScoreConfig(name=DEFAULT_SCORE_NAME, **DEFAULT_SCORE_VALUES))
    elif any(getattr(score, name) != value for name, value in DEFAULT_SCORE_VALUES.items()):
        raise RuntimeError("default score configuration has unexpected weights")
    await ensure_balanced_v2_score_config(session)

    await session.flush()
    return BootstrapResult(
        question_count=len(records),
        dataset_count=len(datasets),
        judge_created=judge_created,
        score_created=score_created,
    )


async def demo_preflight(
    session: AsyncSession,
    settings: Settings,
    *,
    require_snapshot: bool,
) -> list[str]:
    failures: list[str] = []
    question_count = await session.scalar(
        select(func.count())
        .select_from(Question)
        .join(DatasetVersion)
        .where(DatasetVersion.run_eligible.is_(True))
    )
    frozen_dataset_count = await session.scalar(
        select(func.count())
        .select_from(DatasetVersion)
        .where(
            DatasetVersion.status == DatasetStatus.FROZEN,
            DatasetVersion.run_eligible.is_(True),
        )
    )
    if question_count != 100 or frozen_dataset_count != 6:
        failures.append(
            f"demo dataset must contain 100 questions in 6 frozen versions, got "
            f"{question_count} questions and {frozen_dataset_count} versions"
        )

    provider, model, prompt_version = DEFAULT_JUDGE
    judge = await session.scalar(
        select(JudgeConfig).where(
            JudgeConfig.provider == provider,
            JudgeConfig.model == model,
            JudgeConfig.prompt_version == prompt_version,
            JudgeConfig.enabled.is_(True),
        )
    )
    if judge is None:
        failures.append("default DeepSeek judge is missing or disabled")
    score = await session.scalar(select(ScoreConfig).where(ScoreConfig.name == DEFAULT_SCORE_NAME))
    if score is None or any(
        getattr(score, name) != value for name, value in DEFAULT_SCORE_VALUES.items()
    ):
        failures.append("default 40/35/25 score configuration is missing or invalid")
    balanced_score = await session.scalar(
        select(ScoreConfig).where(ScoreConfig.name == BALANCED_V2_SCORE_NAME)
    )
    if balanced_score is None or not has_ability_weights(
        balanced_score, BALANCED_V2_ABILITY_WEIGHTS
    ):
        failures.append("balanced-v2 20/20/20/20/20 score configuration is missing or invalid")

    versions = list(
        await session.scalars(
            select(AgentVersion)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(
                Agent.status == AgentStatus.ENABLED,
                AgentVersion.enabled.is_(True),
                AgentVersion.lifecycle_status == AgentVersionLifecycle.ACTIVE,
            )
            .order_by(AgentVersion.id)
        )
    )
    if len(versions) < 2:
        failures.append(f"at least 2 enabled Agent versions are required, got {len(versions)}")

    cipher: TokenCipher | None = None
    if settings.encryption_key is not None:
        try:
            cipher = TokenCipher.from_secret(settings.encryption_key)
        except ValueError:
            failures.append("Agent token encryption key is invalid")
    for version in versions:
        if version.auth_type != AuthType.BEARER:
            continue
        if version.encrypted_token is None:
            failures.append(f"Agent version {version.id} has no encrypted Bearer token")
        elif cipher is None:
            failures.append("Agent token encryption key is not configured")
            break
        else:
            try:
                cipher.decrypt(version.encrypted_token)
            except (InvalidTag, UnicodeDecodeError, ValueError):
                failures.append(f"Agent version {version.id} token cannot be decrypted")

    if settings.deepseek_api_key is None:
        failures.append("DeepSeek API key is not configured")
    if require_snapshot:
        snapshot = await session.scalar(
            select(LeaderboardSnapshot.id).where(LeaderboardSnapshot.is_current.is_(True))
        )
        if snapshot is None:
            failures.append("a current fallback leaderboard snapshot is required")
    return failures


def validate_dataset_manifest(dataset_path: Path) -> None:
    manifest_path = dataset_path / "manifest.json"
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        benchmarks = manifest["benchmarks"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("demo dataset manifest is invalid") from error
    if manifest.get("question_count") != 100 or not isinstance(benchmarks, dict):
        raise ValueError("demo dataset manifest has unexpected totals")
    expected_files: set[str] = set()
    for entry in benchmarks.values():
        if not isinstance(entry, dict):
            raise ValueError("demo dataset manifest benchmark entry is invalid")
        filename = entry.get("file")
        expected_hash = entry.get("file_sha256")
        if not isinstance(filename, str) or not isinstance(expected_hash, str):
            raise ValueError("demo dataset manifest file metadata is invalid")
        expected_files.add(filename)
        file_path = dataset_path / filename
        try:
            actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError(f"demo dataset file is missing: {filename}") from error
        if actual_hash != expected_hash:
            raise ValueError(f"demo dataset file hash mismatch: {filename}")
    actual_files = {path.name for path in dataset_path.glob("*.jsonl")}
    if actual_files != expected_files:
        raise ValueError("demo dataset files do not match the manifest")
