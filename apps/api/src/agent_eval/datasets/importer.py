from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.datasets.audit import audit_question_records
from agent_eval.datasets.balanced_builder import validate_balanced_v2_quality
from agent_eval.datasets.schema import QuestionRecord, records_content_hash
from agent_eval.db.base import utc_now
from agent_eval.db.enums import RELEASED_ABILITIES, DatasetStatus, Language
from agent_eval.db.models import Benchmark, DatasetVersion, Question
from agent_eval.scoring.configs import ensure_balanced_v2_score_config

LEGACY_COHORT_KEY = "legacy-v1"
BALANCED_V2_COHORT_KEY = "balanced-v2"


class DatasetImportError(ValueError):
    pass


@dataclass(frozen=True)
class ImportedVersion:
    benchmark: str
    version: int
    question_count: int
    content_hash: str
    created: bool


async def import_question_records(
    session: AsyncSession,
    records: list[QuestionRecord],
    *,
    cohort_key: str = LEGACY_COHORT_KEY,
    allow_candidate: bool = False,
) -> list[ImportedVersion]:
    cohort_key = cohort_key.strip()
    if not cohort_key or len(cohort_key) > 80:
        raise DatasetImportError("cohort_key must contain 1 to 80 characters")
    unreleased = sorted(
        {record.ability.value for record in records if record.ability not in RELEASED_ABILITIES}
    )
    if unreleased:
        if not allow_candidate:
            raise DatasetImportError(
                "candidate abilities are audit-only outside the controlled balanced-v2 import: "
                + ", ".join(unreleased)
            )
        if cohort_key != BALANCED_V2_COHORT_KEY:
            raise DatasetImportError("candidate abilities may only use the balanced-v2 cohort")
    if allow_candidate or cohort_key == BALANCED_V2_COHORT_KEY:
        if not allow_candidate:
            raise DatasetImportError("balanced-v2 requires allow_candidate=True")
        report = audit_question_records(records, policy="balanced-v2")
        if not report["policy_passed"]:
            raise DatasetImportError("balanced-v2 import requires a complete passing audit")
        validate_balanced_v2_quality(records, report)
        await ensure_balanced_v2_score_config(session)
    elif cohort_key != LEGACY_COHORT_KEY:
        raise DatasetImportError("generic imports must use the legacy-v1 cohort")
    groups: dict[tuple[str, Language], list[QuestionRecord]] = defaultdict(list)
    for record in records:
        groups[(record.benchmark, record.language)].append(record)

    imported: list[ImportedVersion] = []
    for (benchmark_name, language), group in sorted(groups.items()):
        sample = group[0]
        benchmark = await session.scalar(
            select(Benchmark).where(
                Benchmark.name == benchmark_name,
                Benchmark.language == language,
            )
        )
        if benchmark is None:
            benchmark = Benchmark(
                name=benchmark_name,
                ability=sample.ability,
                language=language,
                source=sample.source,
                license_note=sample.license_note,
            )
            session.add(benchmark)
            await session.flush()
        elif benchmark.ability != sample.ability or benchmark.source != sample.source:
            raise DatasetImportError(
                f"stored metadata does not match import for {benchmark_name}/{language}"
            )

        content_hash = records_content_hash(group)
        existing = await session.scalar(
            select(DatasetVersion).where(
                DatasetVersion.benchmark_id == benchmark.id,
                DatasetVersion.content_hash == content_hash,
            )
        )
        if existing is not None:
            if existing.cohort_key != cohort_key:
                raise DatasetImportError(
                    f"content hash for {benchmark_name} already belongs to "
                    f"cohort {existing.cohort_key}"
                )
            if existing.question_count != len(group):
                raise DatasetImportError(
                    f"stored question count does not match content hash for {benchmark_name}"
                )
            imported.append(
                ImportedVersion(
                    benchmark=benchmark_name,
                    version=existing.version,
                    question_count=existing.question_count,
                    content_hash=content_hash,
                    created=False,
                )
            )
            continue

        next_version = await session.scalar(
            select(func.coalesce(func.max(DatasetVersion.version), 0) + 1).where(
                DatasetVersion.benchmark_id == benchmark.id
            )
        )
        dataset_version = DatasetVersion(
            benchmark_id=benchmark.id,
            version=int(next_version or 1),
            source_version=sample.source_version,
            sampling_seed=sample.sampling_seed,
            content_hash=content_hash,
            question_count=len(group),
            cohort_key=cohort_key,
            status=DatasetStatus.FROZEN,
            frozen_at=utc_now(),
        )
        session.add(dataset_version)
        await session.flush()
        session.add_all(
            [
                Question(
                    dataset_version_id=dataset_version.id,
                    external_id=record.external_id,
                    type=record.type,
                    prompt=record.prompt,
                    choices_json=(
                        [choice.model_dump() for choice in record.choices]
                        if record.choices is not None
                        else None
                    ),
                    reference_answer=record.reference_answer,
                    requires_citations=record.requires_citations,
                    difficulty=record.difficulty,
                    domain=record.domain,
                )
                for record in sorted(group, key=lambda item: item.external_id)
            ]
        )
        imported.append(
            ImportedVersion(
                benchmark=benchmark_name,
                version=dataset_version.version,
                question_count=len(group),
                content_hash=content_hash,
                created=True,
            )
        )

    await session.flush()
    return imported
