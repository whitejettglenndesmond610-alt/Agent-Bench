from collections import defaultdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.base import ExecutableOption

from agent_eval.api.dependencies import (
    DatabaseSession,
    OperatorCsrfSession,
    OperatorSession,
)
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.workspace import request_fingerprint, workspace_error, workspace_user_id
from agent_eval.api.schemas.dataset_releases import (
    DatasetReleaseApprove,
    DatasetReleaseCaseResponse,
    DatasetReleaseCreate,
    DatasetReleaseList,
    DatasetReleasePublish,
    DatasetReleaseResponse,
    DatasetReleaseVersionResponse,
)
from agent_eval.dataset_releases.service import release_manifest_hash
from agent_eval.datasets.schema import QuestionRecord, records_content_hash
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    Ability,
    DatasetReleaseStatus,
    DatasetStatus,
    EvaluationCaseStatus,
    Language,
)
from agent_eval.db.models import (
    Benchmark,
    DatasetRelease,
    DatasetReleaseCase,
    DatasetVersion,
    EvaluationCase,
    Question,
)
from agent_eval.db.models import Session as AuthSession
from agent_eval.evaluation_cases.service import canonical_sha256, record_hash

router = APIRouter(prefix="/api/v1/dataset-releases", tags=["dataset-releases"])


def release_options() -> tuple[ExecutableOption, ...]:
    return (
        selectinload(DatasetRelease.case_links).joinedload(
            DatasetReleaseCase.evaluation_case
        ),
        selectinload(DatasetRelease.dataset_versions).joinedload(DatasetVersion.benchmark),
    )


async def owned_release(
    database: AsyncSession,
    release_id: UUID,
    owner_user_id: UUID,
    *,
    lock: bool = False,
) -> DatasetRelease:
    statement = (
        select(DatasetRelease)
        .options(*release_options())
        .where(
            DatasetRelease.id == release_id,
            DatasetRelease.owner_user_id == owner_user_id,
        )
    )
    if lock:
        statement = statement.with_for_update(of=DatasetRelease)
    release = await database.scalar(statement)
    if release is None:
        raise workspace_error(404, "dataset_release_not_found", "Dataset release not found")
    return release


def release_response(release: DatasetRelease) -> DatasetReleaseResponse:
    cases = [
        DatasetReleaseCaseResponse(
            id=link.evaluation_case.id,
            position=link.position,
            prompt=link.evaluation_case.prompt,
            language=link.evaluation_case.language,
            ability=link.evaluation_case.ability,
            question_type=link.evaluation_case.question_type,
            record_hash=link.record_hash,
        )
        for link in sorted(release.case_links, key=lambda item: item.position)
        if link.evaluation_case.language is not None
        and link.evaluation_case.ability is not None
    ]
    versions = [
        DatasetReleaseVersionResponse(
            id=version.id,
            benchmark_name=version.benchmark.name,
            language=version.benchmark.language,
            ability=version.benchmark.ability,
            version=version.version,
            question_count=version.question_count,
            content_hash=version.content_hash,
            run_eligible=version.run_eligible,
        )
        for version in sorted(
            release.dataset_versions,
            key=lambda item: (item.benchmark.ability.value, item.benchmark.language.value),
        )
    ]
    return DatasetReleaseResponse(
        id=release.id,
        name=release.name,
        description=release.description,
        status=release.status,
        revision=release.revision,
        manifest_hash=release.manifest_hash,
        approved_by_user_id=release.approved_by_user_id,
        approved_at=release.approved_at,
        published_by_user_id=release.published_by_user_id,
        published_at=release.published_at,
        created_at=release.created_at,
        updated_at=release.updated_at,
        cases=cases,
        dataset_versions=versions,
    )


def named_operator_id(auth_session: AuthSession) -> UUID:
    user = auth_session.user
    if user is None or user.is_shared:
        raise workspace_error(
            403,
            "named_operator_required",
            "A named operator account is required for dataset releases",
        )
    return user.id


@router.post("", response_model=DatasetReleaseResponse, status_code=status.HTTP_201_CREATED)
async def create_release(
    payload: DatasetReleaseCreate,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> DatasetReleaseResponse:
    owner_user_id = named_operator_id(auth_session)
    async with sqlite_mutation_guard(
        request, database, f"dataset-release-create:{owner_user_id}"
    ):
        return await create_release_locked(payload, database, owner_user_id)


async def create_release_locked(
    payload: DatasetReleaseCreate,
    database: AsyncSession,
    owner_user_id: UUID,
) -> DatasetReleaseResponse:
    if payload.idempotency_key is None:
        raise workspace_error(
            422, "idempotency_key_required", "An idempotency key is required"
        )
    key = str(payload.idempotency_key)
    fingerprint = request_fingerprint(
        {
            "description": payload.description,
            "evaluation_case_ids": [str(case_id) for case_id in payload.evaluation_case_ids],
            "name": payload.name,
            "operation": "create_dataset_release",
        }
    )
    existing = await database.scalar(
        select(DatasetRelease)
        .options(*release_options())
        .where(
            DatasetRelease.owner_user_id == owner_user_id,
            DatasetRelease.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise workspace_error(409, "idempotency_conflict", "Idempotency key was already used")
        return release_response(existing)

    stored_cases = list(
        await database.scalars(
            select(EvaluationCase)
            .where(
                EvaluationCase.id.in_(payload.evaluation_case_ids),
                EvaluationCase.owner_user_id == owner_user_id,
                EvaluationCase.status == EvaluationCaseStatus.PUBLISHED,
            )
            .with_for_update(of=EvaluationCase)
        )
    )
    by_id = {case.id: case for case in stored_cases}
    if set(by_id) != set(payload.evaluation_case_ids):
        raise workspace_error(
            422,
            "evaluation_cases_not_publishable",
            "Every selected case must be an owned, published evaluation case",
        )
    if any(
        case.record_hash is None or record_hash(case) != case.record_hash
        for case in stored_cases
    ):
        raise workspace_error(409, "evaluation_case_changed", "Published case content changed")

    release = DatasetRelease(
        owner_user_id=owner_user_id,
        name=payload.name,
        description=payload.description,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        status=DatasetReleaseStatus.DRAFT,
        revision=0,
        case_links=[
            DatasetReleaseCase(
                evaluation_case=by_id[case_id],
                position=position,
                record_hash=by_id[case_id].record_hash or "",
            )
            for position, case_id in enumerate(payload.evaluation_case_ids, start=1)
        ],
        dataset_versions=[],
    )
    database.add(release)
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await database.scalar(
            select(DatasetRelease)
            .options(*release_options())
            .where(
                DatasetRelease.owner_user_id == owner_user_id,
                DatasetRelease.idempotency_key == key,
            )
        )
        if duplicate is not None and duplicate.request_fingerprint == fingerprint:
            return release_response(duplicate)
        raise workspace_error(
            409, "dataset_release_conflict", "Dataset release changed concurrently"
        ) from error
    return release_response(release)


@router.get("", response_model=DatasetReleaseList)
async def list_releases(
    database: DatabaseSession,
    auth_session: OperatorSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DatasetReleaseList:
    releases = list(
        await database.scalars(
            select(DatasetRelease)
            .options(*release_options())
            .where(DatasetRelease.owner_user_id == workspace_user_id(auth_session))
            .order_by(DatasetRelease.updated_at.desc(), DatasetRelease.id.desc())
            .limit(limit)
        )
    )
    return DatasetReleaseList(items=[release_response(release) for release in releases])


@router.get("/{release_id}", response_model=DatasetReleaseResponse)
async def get_release(
    release_id: UUID,
    database: DatabaseSession,
    auth_session: OperatorSession,
) -> DatasetReleaseResponse:
    return release_response(
        await owned_release(database, release_id, workspace_user_id(auth_session))
    )


@router.post("/{release_id}/approve", response_model=DatasetReleaseResponse)
async def approve_release(
    release_id: UUID,
    payload: DatasetReleaseApprove,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> DatasetReleaseResponse:
    owner_user_id = named_operator_id(auth_session)
    async with sqlite_mutation_guard(request, database, f"dataset-release:{release_id}"):
        release = await owned_release(database, release_id, owner_user_id, lock=True)
        if release.revision != payload.expected_revision:
            raise workspace_error(409, "revision_conflict", "Dataset release revision changed")
        if release.status != DatasetReleaseStatus.DRAFT:
            raise workspace_error(409, "dataset_release_not_draft", "Only drafts can be approved")
        for link in release.case_links:
            case = link.evaluation_case
            if (
                case.status != EvaluationCaseStatus.PUBLISHED
                or case.record_hash is None
                or case.record_hash != link.record_hash
                or record_hash(case) != link.record_hash
                or canonical_sha256(case.source_payload_json) != case.source_hash
            ):
                raise workspace_error(
                    409, "evaluation_case_changed", "A selected evaluation case changed"
                )
        release.manifest_hash = release_manifest_hash(release)
        release.approved_by_user_id = owner_user_id
        release.approved_at = utc_now()
        release.status = DatasetReleaseStatus.APPROVED
        release.revision += 1
        release.updated_at = utc_now()
        await database.commit()
        return release_response(release)


def question_record(
    release: DatasetRelease, case: EvaluationCase, benchmark_name: str
) -> QuestionRecord:
    if case.language is None or case.ability is None or case.reference_answer is None:
        raise workspace_error(409, "evaluation_case_incomplete", "Published case is incomplete")
    return QuestionRecord(
        benchmark=benchmark_name,
        source=f"evaluation-case-release:{release.id}",
        source_version=f"release-{release.id}",
        license_note="Operator-reviewed curated evaluation cases",
        sampling_seed=0,
        external_id=f"case-{case.id}",
        language=case.language,
        ability=case.ability,
        type=case.question_type,
        prompt=case.prompt,
        reference_answer=case.reference_answer,
        choices=None,
        requires_citations=case.requires_citations,
        difficulty=case.difficulty,
        domain=case.domain,
    )


@router.post("/{release_id}/publish", response_model=DatasetReleaseResponse)
async def publish_release(
    release_id: UUID,
    payload: DatasetReleasePublish,
    request: Request,
    database: DatabaseSession,
    auth_session: OperatorCsrfSession,
) -> DatasetReleaseResponse:
    owner_user_id = named_operator_id(auth_session)
    async with sqlite_mutation_guard(request, database, f"dataset-release:{release_id}"):
        return await publish_release_locked(
            release_id, payload, database, owner_user_id
        )


async def publish_release_locked(
    release_id: UUID,
    payload: DatasetReleasePublish,
    database: AsyncSession,
    owner_user_id: UUID,
) -> DatasetReleaseResponse:
    release = await owned_release(database, release_id, owner_user_id, lock=True)
    if release.revision != payload.expected_revision:
        raise workspace_error(409, "revision_conflict", "Dataset release revision changed")
    if release.status != DatasetReleaseStatus.APPROVED or release.manifest_hash is None:
        raise workspace_error(
            409, "dataset_release_not_approved", "Approve the release before publishing"
        )
    if release_manifest_hash(release) != release.manifest_hash:
        raise workspace_error(409, "dataset_release_changed", "Approved manifest changed")
    case_ids = [link.evaluation_case_id for link in release.case_links]
    existing_case_id = await database.scalar(
        select(Question.source_evaluation_case_id)
        .where(Question.source_evaluation_case_id.in_(case_ids))
        .limit(1)
    )
    if existing_case_id is not None:
        raise workspace_error(
            409,
            "evaluation_case_already_released",
            "A selected case already belongs to a formal dataset",
        )

    groups: dict[tuple[Ability, Language], list[EvaluationCase]] = defaultdict(list)
    for link in release.case_links:
        case = link.evaluation_case
        if (
            case.status != EvaluationCaseStatus.PUBLISHED
            or case.record_hash != link.record_hash
            or record_hash(case) != link.record_hash
            or case.language is None
            or case.ability is None
        ):
            raise workspace_error(409, "evaluation_case_changed", "A selected case changed")
        groups[(case.ability, case.language)].append(case)

    now = utc_now()
    cohort_key = f"curated-{release.id.hex}"
    for (ability, language), cases in sorted(
        groups.items(), key=lambda item: (item[0][0].value, item[0][1].value)
    ):
        benchmark_name = f"Curated-{release.id.hex[:8]}-{ability.value}"
        benchmark = Benchmark(
            name=benchmark_name,
            ability=ability,
            language=language,
            source=f"evaluation-case-release:{release.id}",
            license_note="Operator-reviewed curated evaluation cases",
        )
        records = [question_record(release, case, benchmark_name) for case in cases]
        version = DatasetVersion(
            benchmark=benchmark,
            dataset_release=release,
            version=1,
            source_version=f"release-{release.id}",
            sampling_seed=0,
            content_hash=records_content_hash(records),
            question_count=len(records),
            cohort_key=cohort_key,
            status=DatasetStatus.FROZEN,
            run_eligible=False,
            frozen_at=now,
        )
        version.questions = [
            Question(
                external_id=record.external_id,
                type=record.type,
                prompt=record.prompt,
                choices_json=None,
                reference_answer=record.reference_answer,
                requires_citations=record.requires_citations,
                difficulty=record.difficulty,
                domain=record.domain,
                source_evaluation_case=case,
            )
            for case, record in zip(cases, records, strict=True)
        ]
        database.add(version)

    release.published_by_user_id = owner_user_id
    release.published_at = now
    release.status = DatasetReleaseStatus.PUBLISHED
    release.revision += 1
    release.updated_at = now
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        raise workspace_error(
            409, "dataset_release_conflict", "Dataset release could not be published"
        ) from error
    return release_response(release)
