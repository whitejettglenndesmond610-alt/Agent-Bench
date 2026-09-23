"""tau2-bench dataset importer.

Builds QuestionRecord entries from tau2-bench task files (retail, telecom and
airline domains) and imports them under the ``tau2-v1`` cohort. tau2 is
imported lazily inside the builder so this module stays importable without the
tau2 dependency group.
"""

import hashlib
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.datasets.importer import (
    TAU2_V1_COHORT_KEY,
    ImportedVersion,
    import_question_records,
)
from agent_eval.datasets.schema import QuestionRecord
from agent_eval.db.enums import Ability, Language, QuestionType

TAU2_RETAIL_BENCHMARK = "Tau2-Retail"
TAU2_SOURCE = "tau2-bench"
TAU2_VERSION = "1.0.1"
TAU2_TASK_SPLIT = "base"
SUPPORTED_DOMAINS = ("retail", "telecom", "airline")
# None imports the full base split (114/114/50 tasks in pinned tau2 v1.0.1).
DEFAULT_IMPORT_LIMIT: int | None = None


def tau2_benchmark_name(domain: str) -> str:
    return f"Tau2-{domain.capitalize()}"


def tau2_external_id(domain: str, task_id: str) -> str:
    """external_id is capped at 200 chars; long composite ids (telecom) get a
    deterministic hash suffix so readability is kept whenever it fits."""
    candidate = f"{domain}-{task_id}"
    if len(candidate) <= 200:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:12]
    return f"{candidate[:187]}-{digest}"


def build_tau2_records(
    data_dir: Path, domain: str, *, limit: int | None = None
) -> list[QuestionRecord]:
    """Load tau2 tasks for one domain from a tau2-bench data directory.

    ``data_dir`` is the directory that contains ``tau2/domains/<domain>`` (the
    same layout TAU2_DATA_DIR points at). Tasks are taken from the "base"
    split in task-id order; ``limit`` caps how many tasks are imported.
    """
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(
            f"unsupported tau2 domain {domain!r}; expected one of {SUPPORTED_DOMAINS}"
        )
    os.environ["TAU2_DATA_DIR"] = str(data_dir)
    from tau2.registry import registry

    tasks = sorted(
        registry.get_tasks_loader(domain)(TAU2_TASK_SPLIT),
        key=lambda task: task.id,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        raise ValueError(f"no tau2 {domain} tasks found under {data_dir}")
    records: list[QuestionRecord] = []
    for task in tasks:
        records.append(
            QuestionRecord(
                benchmark=tau2_benchmark_name(domain),
                source=TAU2_SOURCE,
                source_version=f"tau2-{TAU2_VERSION}",
                license_note="tau2-bench (Sierra Research), MIT license",
                sampling_seed=0,
                external_id=tau2_external_id(domain, task.id),
                language=Language.EN,
                ability=Ability.AGENTIC,
                type=QuestionType.INTERACTIVE,
                prompt=str(task.user_scenario.instructions),
                reference_answer=f"tau2:{domain}:{task.id}",
                requires_citations=False,
                domain=domain,
            )
        )
    return records


def build_tau2_retail_records(
    data_dir: Path, *, limit: int | None = None
) -> list[QuestionRecord]:
    """Compatibility wrapper for the original retail-only builder."""
    return build_tau2_records(data_dir, "retail", limit=limit)


async def import_tau2(
    session: AsyncSession,
    *,
    domain: str,
    data_dir: Path,
    limit: int | None = DEFAULT_IMPORT_LIMIT,
) -> list[ImportedVersion]:
    records = build_tau2_records(data_dir, domain, limit=limit)
    return await import_question_records(
        session,
        records,
        cohort_key=TAU2_V1_COHORT_KEY,
        held_out=False,
    )


async def import_tau2_retail(
    session: AsyncSession,
    *,
    data_dir: Path,
    limit: int | None = DEFAULT_IMPORT_LIMIT,
) -> list[ImportedVersion]:
    """Compatibility wrapper for the original retail-only import."""
    return await import_tau2(session, domain="retail", data_dir=data_dir, limit=limit)
