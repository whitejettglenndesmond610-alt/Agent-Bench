import asyncio
import base64
import hashlib
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from agent_eval.core.settings import Settings
from agent_eval.datasets.audit import audit_question_records
from agent_eval.datasets.balanced_builder import build_balanced_v2_dataset
from agent_eval.datasets.demo_builder import decrypt_browsecomp
from agent_eval.datasets.importer import (
    BALANCED_V2_COHORT_KEY,
    LEGACY_COHORT_KEY,
    DatasetImportError,
    import_question_records,
)
from agent_eval.datasets.schema import (
    Choice,
    DatasetValidationError,
    QuestionRecord,
    load_question_records,
    records_content_hash,
    validate_demo_distribution,
)
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import Ability, DatasetStatus, Language, QuestionType
from agent_eval.db.models import Benchmark, DatasetVersion, Question, ScoreConfig
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.runs.profiles import (
    UNIFIED_V3_EXPECTED_COUNTS,
    UNIFIED_V3_PROFILE_KEY,
    UNIFIED_V3_SPECIALIST_PROFILE_KEYS,
    build_run_profiles,
)
from agent_eval.scoring.configs import BALANCED_V2_SCORE_NAME

DEMO_BENCHMARKS = [
    ("C-Eval", Language.ZH, Ability.REASONING, 30),
    ("MMLU-Pro", Language.EN, Ability.REASONING, 10),
    ("BrowseComp-ZH", Language.ZH, Ability.RETRIEVAL, 20),
    ("BrowseComp", Language.EN, Ability.RETRIEVAL, 10),
    ("BizFinBench", Language.ZH, Ability.DATA_REASONING, 20),
    ("FinQA", Language.EN, Ability.DATA_REASONING, 10),
]


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def demo_records() -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for benchmark, language, ability, count in DEMO_BENCHMARKS:
        for index in range(count):
            question_type = QuestionType.SHORT_ANSWER
            choices = None
            reference_answer = f"answer-{index}"
            if ability == Ability.REASONING:
                question_type = QuestionType.MULTIPLE_CHOICE
                choices = [Choice(label="A", text="first"), Choice(label="B", text="second")]
                reference_answer = "A"
            elif benchmark == "BizFinBench" and index >= count // 2:
                question_type = QuestionType.OPEN_ENDED

            records.append(
                QuestionRecord(
                    benchmark=benchmark,
                    source=f"https://example.test/{benchmark.lower()}",
                    source_version="pinned-revision",
                    license_note="Public benchmark; attribution required",
                    sampling_seed=20260716,
                    external_id=f"{benchmark.lower()}-{index:03d}",
                    language=language,
                    ability=ability,
                    type=question_type,
                    prompt=f"Original question {index} from {benchmark}",
                    reference_answer=reference_answer,
                    choices=choices,
                    requires_citations=ability == Ability.RETRIEVAL,
                    domain="test-domain",
                )
            )
    return records


def test_demo_distribution_and_hash_are_deterministic() -> None:
    records = demo_records()

    validate_demo_distribution(records)

    assert records_content_hash(records) == records_content_hash(list(reversed(records)))


def test_dataset_audit_is_deterministic_and_reports_balanced_v2_gaps() -> None:
    records = load_question_records(Path("data/demo-dataset"))

    report = audit_question_records(records, policy="balanced-v2")
    reversed_report = audit_question_records(list(reversed(records)), policy="balanced-v2")

    assert report == reversed_report
    assert report["question_count"] == 100
    assert len(report["canonical_bundle_hash"]) == 64
    assert report["distributions"]["ability"] == {
        "data_reasoning": 30,
        "reasoning": 40,
        "retrieval": 30,
    }
    assert report["distributions"]["language"] == {"en": 30, "zh": 70}
    assert report["metadata_coverage"]["difficulty"] == {
        "present": 10,
        "missing": 90,
        "percent": 10.0,
    }
    assert report["prompt_characters"]["max"] == 61_791
    assert report["policy_passed"] is False
    dimension_gaps = {
        violation["dimension"]: violation["actual"]
        for violation in report["violations"]
        if violation["code"] == "dimension_count"
    }
    assert dimension_gaps == {
        "instruction_following": 0,
        "long_context": 0,
        "reasoning": 40,
    }


def test_balanced_v2_builder_is_deterministic_and_passes_release_policy(
    tmp_path: Path,
) -> None:
    output = tmp_path / "balanced-v2"

    first = build_balanced_v2_dataset(Path("data/demo-dataset"), output)
    records = load_question_records(output)
    report = audit_question_records(records, policy="balanced-v2")
    second = build_balanced_v2_dataset(Path("data/demo-dataset"), output)

    assert first == second
    assert first["question_count"] == 150
    assert first["canonical_bundle_hash"] == report["canonical_bundle_hash"]
    assert first["quality"] == {
        "status": "automated_pass",
        "schema_validated": 150,
        "official_reference_anchors": 75,
        "deterministic_original_answers": 75,
        "normalized_prompt_duplicates": 0,
        "long_context_min_characters": first["quality"]["long_context_min_characters"],
        "human_release_review_required": True,
    }
    assert first["quality"]["long_context_min_characters"] > 8000
    assert report["policy_passed"] is True
    assert report["violations"] == []
    assert report["distributions"]["ability"] == {
        "data_reasoning": 30,
        "instruction_following": 30,
        "long_context": 30,
        "reasoning": 30,
        "retrieval": 30,
    }
    assert report["distributions"]["language"] == {"en": 75, "zh": 75}
    assert report["distributions"]["difficulty"] == {
        "easy": 30,
        "hard": 30,
        "medium": 90,
    }
    assert first["release_scope"] == "internal_research_only"
    assert first["redistributable"] is False
    assert {review["decision"] for review in first["source_reviews"]} >= {
        "not_selected",
        "local_only",
        "approved_internal_original",
    }


def test_browsecomp_decryption_matches_official_algorithm() -> None:
    plaintext = "A benchmark question with 中文"
    canary = "demo-canary"
    key = hashlib.sha256(canary.encode()).digest()
    encrypted = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(plaintext.encode()))

    assert decrypt_browsecomp(base64.b64encode(encrypted).decode(), canary) == plaintext


def test_question_schema_rejects_invalid_shapes() -> None:
    with pytest.raises(ValidationError, match="at least two choices"):
        QuestionRecord(
            benchmark="Invalid",
            source="https://example.test",
            source_version="v1",
            sampling_seed=1,
            external_id="1",
            language=Language.EN,
            ability=Ability.REASONING,
            type=QuestionType.MULTIPLE_CHOICE,
            prompt="Question",
            reference_answer="A",
        )

    with pytest.raises(ValidationError, match="must require citations"):
        QuestionRecord(
            benchmark="Invalid",
            source="https://example.test",
            source_version="v1",
            sampling_seed=1,
            external_id="1",
            language=Language.EN,
            ability=Ability.RETRIEVAL,
            type=QuestionType.SHORT_ANSWER,
            prompt="Question",
            reference_answer="Answer",
        )

    valid = demo_records()[0].model_dump(mode="json")
    valid["prompt"] = "   "
    with pytest.raises(ValidationError, match="at least 1 character"):
        QuestionRecord.model_validate(valid)

    valid = demo_records()[0].model_dump(mode="json")
    valid["untracked_dimension"] = "ignored-before-audit"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QuestionRecord.model_validate(valid)


def test_jsonl_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    record = demo_records()[0]
    path = tmp_path / "duplicate.jsonl"
    line = record.model_dump_json()
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(DatasetValidationError, match="duplicate external_id"):
        load_question_records(path)


async def test_dataset_import_is_frozen_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "datasets.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    records = demo_records()

    try:
        async with session_scope(session_factory) as session:
            first = await import_question_records(session, records)
        async with session_scope(session_factory) as session:
            second = await import_question_records(session, list(reversed(records)))

        async with session_factory() as session:
            benchmark_count = await session.scalar(select(func.count()).select_from(Benchmark))
            version_count = await session.scalar(select(func.count()).select_from(DatasetVersion))
            question_count = await session.scalar(select(func.count()).select_from(Question))
            statuses = set(await session.scalars(select(DatasetVersion.status)))

        assert len(first) == len(second) == 6
        assert all(item.created for item in first)
        assert not any(item.created for item in second)
        assert benchmark_count == 6
        assert version_count == 6
        assert question_count == 100
        assert statuses == {DatasetStatus.FROZEN}
    finally:
        await engine.dispose()


async def test_unreleased_balanced_abilities_cannot_be_imported(tmp_path: Path) -> None:
    output = tmp_path / "balanced-v2"
    build_balanced_v2_dataset(Path("data/demo-dataset"), output)
    candidate = next(
        record
        for record in load_question_records(output)
        if record.ability == Ability.INSTRUCTION_FOLLOWING
    )
    database_path = tmp_path / "unreleased.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            with pytest.raises(DatasetImportError, match="audit-only"):
                await import_question_records(session, [candidate])
    finally:
        await engine.dispose()


async def test_balanced_v2_import_is_audited_isolated_and_profiled_atomically(
    tmp_path: Path,
) -> None:
    output = tmp_path / "balanced-v2"
    build_balanced_v2_dataset(Path("data/demo-dataset"), output)
    records = load_question_records(output)
    database_path = tmp_path / "balanced-import.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            with pytest.raises(DatasetImportError, match="complete passing audit"):
                await import_question_records(
                    session,
                    records[:-1],
                    cohort_key=BALANCED_V2_COHORT_KEY,
                    allow_candidate=True,
                )
            await session.rollback()

        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session,
                records,
                cohort_key=BALANCED_V2_COHORT_KEY,
                allow_candidate=True,
            )
        async with session_factory() as session:
            profiles = {profile.key: profile for profile in await build_run_profiles(session)}
            cohorts = set(await session.scalars(select(DatasetVersion.cohort_key)))
            score = await session.scalar(
                select(ScoreConfig).where(ScoreConfig.name == BALANCED_V2_SCORE_NAME)
            )

        assert sum(item.question_count for item in imported) == 150
        assert cohorts == {BALANCED_V2_COHORT_KEY}
        assert score is not None
        assert (
            score.reasoning_weight,
            score.retrieval_weight,
            score.data_weight,
            score.instruction_following_weight,
            score.long_context_weight,
        ) == (20, 20, 20, 20, 20)
        assert profiles["balanced-v2"].question_count == 150
        assert set(profiles["balanced-v2"].publishes_to) == {
            "overall",
            *(ability.value for ability in Ability if ability is not Ability.AGENTIC),
        }
        assert profiles["comprehensive"].dataset_version_ids == ()
    finally:
        await engine.dispose()


async def test_unified_v3_composes_frozen_cohorts_without_duplicate_questions(
    tmp_path: Path,
) -> None:
    output = tmp_path / "balanced-v2"
    build_balanced_v2_dataset(Path("data/demo-dataset"), output)
    database_path = tmp_path / "cohorts.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            session.add(ScoreConfig(name="Legacy"))
            await import_question_records(
                session, load_question_records(Path("data/demo-dataset"))
            )
            await import_question_records(
                session,
                load_question_records(output),
                cohort_key=BALANCED_V2_COHORT_KEY,
                allow_candidate=True,
            )
        async with session_factory() as session:
            profiles = {profile.key: profile for profile in await build_run_profiles(session)}
            versions = {
                version.id: version
                for version in await session.scalars(
                    select(DatasetVersion).options(joinedload(DatasetVersion.benchmark))
                )
            }

        legacy = profiles["comprehensive"]
        balanced = profiles["balanced-v2"]
        unified = profiles[UNIFIED_V3_PROFILE_KEY]
        assert legacy.question_count == 100
        assert balanced.question_count == 150
        assert unified.question_count == 175
        assert unified.benchmark_count == 13
        assert unified.ability_weights == {
            ability: 20 for ability in Ability if ability is not Ability.AGENTIC
        }
        assert unified.ranking_eligible is True
        assert set(unified.dataset_version_ids) == {
            *legacy.dataset_version_ids,
            *(
                version_id
                for version_id in balanced.dataset_version_ids
                if versions[version_id].benchmark.name.startswith("BalancedV2-Original-")
                or versions[version_id].benchmark.name
                in {
                    "BalancedV2-Instruction-Following",
                    "BalancedV2-Long-Context",
                }
            ),
        }
        for ability, expected_count in UNIFIED_V3_EXPECTED_COUNTS.items():
            specialist = profiles[UNIFIED_V3_SPECIALIST_PROFILE_KEYS[ability]]
            assert specialist.question_count == expected_count
            assert specialist.target_ability == ability
            assert specialist.ability_weights == {
                    item: 20 for item in Ability if item is not Ability.AGENTIC
                }
            assert all(
                versions[version_id].benchmark.ability == ability
                for version_id in specialist.dataset_version_ids
            )
        assert {
            versions[version_id].cohort_key for version_id in legacy.dataset_version_ids
        } == {LEGACY_COHORT_KEY}
        assert {
            versions[version_id].cohort_key for version_id in balanced.dataset_version_ids
        } == {BALANCED_V2_COHORT_KEY}
        original_ids = set(unified.dataset_version_ids) - set(legacy.dataset_version_ids)

        async with session_scope(session_factory) as session:
            duplicate = await session.scalar(
                select(Question)
                .where(Question.dataset_version_id.in_(original_ids))
                .limit(1)
            )
            legacy_question = await session.scalar(
                select(Question)
                .join(DatasetVersion)
                .where(DatasetVersion.cohort_key == LEGACY_COHORT_KEY)
                .limit(1)
            )
            assert duplicate is not None and legacy_question is not None
            duplicate.prompt = f"  {legacy_question.prompt.upper()}  "
        async with session_factory() as session:
            incomplete = {profile.key for profile in await build_run_profiles(session)}
        assert UNIFIED_V3_PROFILE_KEY not in incomplete
        assert not incomplete.intersection(UNIFIED_V3_SPECIALIST_PROFILE_KEYS.values())
    finally:
        await engine.dispose()
