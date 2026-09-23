import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.sql.base import ExecutableOption

from agent_eval.agents.inference import MAX_RESPONSE_BYTES
from agent_eval.agents.mapping import extract_answer
from agent_eval.api.schemas.runs import (
    EvaluationRunEvidenceList,
    RunEvidenceArtifactMetadata,
    RunEvidenceAttempt,
    RunEvidenceCandidateAnswerStatus,
    RunEvidenceCitation,
    RunEvidenceDetail,
    RunEvidenceListItem,
    RunEvidenceQuestionChoice,
    RunEvidenceScoreDetail,
    RunEvidenceScoreSummary,
)
from agent_eval.core.artifact_store import ArtifactStore
from agent_eval.core.artifacts import read_indexed_artifact
from agent_eval.db.base import ensure_utc, utc_now
from agent_eval.db.enums import Ability, ArtifactKind, ExecutionStatus, Language, QuestionType
from agent_eval.db.models import (
    AgentVersion,
    ArtifactIndex,
    Benchmark,
    CitationRecord,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    Question,
    RunAgent,
    ScoreRecord,
)


class RunEvidenceNotFoundError(LookupError):
    pass


# Trajectory artifacts are bounded at write time by the agent response cap per
# turn, but a long episode can still grow large; cap evidence replay payloads.
MAX_TRAJECTORY_BYTES = 2 * MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class RunEvidenceFilters:
    run_agent_id: UUID | None = None
    status: ExecutionStatus | None = None
    ability: Ability | None = None
    language: Language | None = None
    error_type: str | None = None
    min_score: float | None = None
    max_score: float | None = None


async def list_run_evidence(
    database: AsyncSession,
    run_id: UUID,
    *,
    filters: RunEvidenceFilters,
    limit: int,
    offset: int,
) -> EvaluationRunEvidenceList:
    if await database.scalar(select(EvaluationRun.id).where(EvaluationRun.id == run_id)) is None:
        raise RunEvidenceNotFoundError

    conditions = [RunAgent.run_id == run_id]
    if filters.run_agent_id is not None:
        conditions.append(ExecutionItem.run_agent_id == filters.run_agent_id)
    if filters.status is not None:
        conditions.append(ExecutionItem.status == filters.status)
    if filters.ability is not None:
        conditions.append(Benchmark.ability == filters.ability)
    if filters.language is not None:
        conditions.append(Benchmark.language == filters.language)
    if filters.error_type is not None:
        conditions.append(ExecutionItem.error_type == filters.error_type)
    if filters.min_score is not None:
        conditions.append(ScoreRecord.final_score >= filters.min_score)
    if filters.max_score is not None:
        conditions.append(ScoreRecord.final_score <= filters.max_score)

    joined = (
        select(ExecutionItem)
        .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
        .join(Question, Question.id == ExecutionItem.question_id)
        .join(DatasetVersion, DatasetVersion.id == Question.dataset_version_id)
        .join(Benchmark, Benchmark.id == DatasetVersion.benchmark_id)
        .outerjoin(ScoreRecord, ScoreRecord.execution_item_id == ExecutionItem.id)
    )
    total = await database.scalar(
        select(func.count(ExecutionItem.id))
        .select_from(ExecutionItem)
        .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
        .join(Question, Question.id == ExecutionItem.question_id)
        .join(DatasetVersion, DatasetVersion.id == Question.dataset_version_id)
        .join(Benchmark, Benchmark.id == DatasetVersion.benchmark_id)
        .outerjoin(ScoreRecord, ScoreRecord.execution_item_id == ExecutionItem.id)
        .where(*conditions)
    )
    items = list(
        await database.scalars(
            joined.options(*_summary_options())
            .where(*conditions)
            .order_by(ExecutionItem.created_at, ExecutionItem.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return EvaluationRunEvidenceList(
        run_id=run_id,
        items=[_list_item(item) for item in items],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


async def get_run_evidence_detail(
    database: AsyncSession,
    artifact_store: ArtifactStore,
    run_id: UUID,
    execution_item_id: UUID,
) -> RunEvidenceDetail:
    item = await database.scalar(
        select(ExecutionItem)
        .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
        .options(*_detail_options())
        .where(
            ExecutionItem.id == execution_item_id,
            RunAgent.run_id == run_id,
        )
    )
    if item is None:
        raise RunEvidenceNotFoundError

    candidate_answer, answer_status = await _reconstruct_candidate_answer(artifact_store, item)
    summary = _list_item(item).model_dump()
    summary["score"] = _score_detail(item.score)
    question = item.question
    held_out = question.dataset_version.held_out
    # Held-out masking applies to the trajectory as well: its first user
    # message is derived from the same masked prompt text, so interactive
    # held-out items must not leak it through the replay payload.
    trajectory = None
    if not held_out and question.type == QuestionType.INTERACTIVE:
        trajectory = await _load_trajectory(artifact_store, item)
    return RunEvidenceDetail(
        **summary,
        prompt=None if held_out else question.prompt,
        choices=(
            None
            if held_out or question.choices_json is None
            else [
                RunEvidenceQuestionChoice.model_validate(choice)
                for choice in question.choices_json
            ]
        ),
        reference_answer=None if held_out else question.reference_answer,
        held_out=held_out,
        candidate_answer=candidate_answer,
        candidate_answer_status=answer_status,
        attempts=[
            RunEvidenceAttempt(
                attempt_id=attempt.id,
                attempt_no=attempt.attempt_no,
                status=attempt.status,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                status_code=attempt.status_code,
                latency_ms=attempt.latency_ms,
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                total_tokens=attempt.total_tokens,
                error_type=attempt.error_type,
            )
            for attempt in sorted(item.attempts, key=lambda value: (value.attempt_no, value.id))
        ],
        citations=[
            _citation(citation)
            for citation in sorted(item.citations, key=lambda value: (value.position, value.id))
        ],
        response_artifact=_artifact_metadata(item.artifact),
        trajectory=trajectory,
    )


def _summary_options() -> tuple[ExecutableOption, ...]:
    return (
        joinedload(ExecutionItem.run_agent)
        .joinedload(RunAgent.agent_version)
        .joinedload(AgentVersion.agent),
        joinedload(ExecutionItem.question)
        .joinedload(Question.dataset_version)
        .joinedload(DatasetVersion.benchmark),
        joinedload(ExecutionItem.score),
    )


def _detail_options() -> tuple[ExecutableOption, ...]:
    return (
        *_summary_options(),
        joinedload(ExecutionItem.artifact),
        joinedload(ExecutionItem.score).joinedload(ScoreRecord.judge_artifact),
        selectinload(ExecutionItem.attempts),
        selectinload(ExecutionItem.citations).joinedload(CitationRecord.artifact),
        selectinload(ExecutionItem.citations).joinedload(CitationRecord.judge_artifact),
    )


def _list_item(item: ExecutionItem) -> RunEvidenceListItem:
    run_agent = item.run_agent
    version = run_agent.agent_version
    question = item.question
    benchmark = question.dataset_version.benchmark
    return RunEvidenceListItem(
        execution_item_id=item.id,
        run_agent_id=run_agent.id,
        agent_id=version.agent_id,
        agent_version_id=version.id,
        agent_name=version.agent.name,
        provider=version.agent.provider,
        version=version.version,
        question_id=question.id,
        question_external_id=question.external_id,
        question_type=question.type,
        benchmark_id=benchmark.id,
        benchmark_name=benchmark.name,
        ability=benchmark.ability,
        language=benchmark.language,
        requires_citations=question.requires_citations,
        difficulty=question.difficulty,
        domain=question.domain,
        status=item.status,
        error_type=item.error_type,
        answer_preview=item.answer_preview,
        attempt_count=item.attempt_count,
        latency_ms=item.latency_ms,
        score=_score_summary(item.score),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _score_summary(score: ScoreRecord | None) -> RunEvidenceScoreSummary | None:
    if score is None:
        return None
    return RunEvidenceScoreSummary(
        score_id=score.id,
        answer_score=float(score.answer_score),
        citation_score=float(score.citation_score) if score.citation_score is not None else None,
        final_score=float(score.final_score),
    )


def _score_detail(score: ScoreRecord | None) -> RunEvidenceScoreDetail | None:
    summary = _score_summary(score)
    if score is None or summary is None:
        return None
    return RunEvidenceScoreDetail(
        **summary.model_dump(),
        judge_reason=score.judge_reason,
        judge_artifact=_artifact_metadata(score.judge_artifact),
        judge_provider=score.judge_provider,
        judge_model_requested=score.judge_model_requested,
        judge_model_returned=score.judge_model_returned,
        judge_prompt_version=score.judge_prompt_version,
        judge_temperature=(
            float(score.judge_temperature) if score.judge_temperature is not None else None
        ),
        judge_details=score.judge_details_json,
    )


def _citation(citation: CitationRecord) -> RunEvidenceCitation:
    return RunEvidenceCitation(
        citation_id=citation.id,
        position=citation.position,
        original_url=citation.original_url,
        final_url=citation.final_url,
        fetch_status=citation.fetch_status,
        http_status=citation.http_status,
        authenticity_score=(
            float(citation.authenticity_score)
            if citation.authenticity_score is not None
            else None
        ),
        support_score=(
            float(citation.support_score) if citation.support_score is not None else None
        ),
        judge_reason=citation.judge_reason,
        content_artifact=_artifact_metadata(citation.artifact),
        judge_artifact=_artifact_metadata(citation.judge_artifact),
    )


def _artifact_metadata(artifact: ArtifactIndex | None) -> RunEvidenceArtifactMetadata | None:
    if artifact is None:
        return None
    if len(artifact.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in artifact.sha256
    ):
        return None
    return RunEvidenceArtifactMetadata(
        artifact_id=artifact.id,
        kind=artifact.kind,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        expires_at=artifact.expires_at,
        retained=ensure_utc(artifact.expires_at) > utc_now(),
    )


async def _load_trajectory(
    artifact_store: ArtifactStore,
    item: ExecutionItem,
) -> dict[str, Any] | None:
    """Read and parse the tau2 trajectory artifact; degrade to None on failure."""
    artifact = item.artifact
    if artifact is None or artifact.kind != ArtifactKind.TRAJECTORY:
        return None
    if ensure_utc(artifact.expires_at) <= utc_now():
        return None
    if artifact.size_bytes > MAX_TRAJECTORY_BYTES:
        return None
    try:
        content = await asyncio.to_thread(
            read_indexed_artifact,
            artifact_store,
            relative_path=artifact.relative_path,
            expected_sha256=artifact.sha256,
            expected_size=artifact.size_bytes,
        )
        trajectory = json.loads(content)
    except Exception:
        return None
    return trajectory if isinstance(trajectory, dict) else None


async def _reconstruct_candidate_answer(
    artifact_store: ArtifactStore,
    item: ExecutionItem,
) -> tuple[str | None, RunEvidenceCandidateAnswerStatus]:
    artifact = item.artifact
    if artifact is None or item.answer_hash is None:
        return None, "not_recorded"
    if ensure_utc(artifact.expires_at) <= utc_now():
        return None, "expired"
    if artifact.kind != ArtifactKind.AGENT_RESPONSE or artifact.size_bytes > MAX_RESPONSE_BYTES:
        return None, "unavailable"
    try:
        content = await asyncio.to_thread(
            read_indexed_artifact,
            artifact_store,
            relative_path=artifact.relative_path,
            expected_sha256=artifact.sha256,
            expected_size=artifact.size_bytes,
        )
        candidate_answer = extract_answer(
            json.loads(content), item.run_agent.agent_version.answer_jsonpath
        )
    except Exception:
        return None, "unavailable"
    digest = hashlib.sha256(candidate_answer.encode()).hexdigest()
    if not hmac.compare_digest(digest, item.answer_hash):
        return None, "unavailable"
    return candidate_answer, "available"
