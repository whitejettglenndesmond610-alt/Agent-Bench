import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from test_quality import login_operator as quality_login
from test_quality import quality_client, seed_quality_finding
from test_run_evidence import create_run, seed_evidence
from test_runs import login_operator, run_client

from agent_eval.core.settings import Settings
from agent_eval.datasets.importer import (
    HELDOUT_V1_COHORT_KEY,
    LEGACY_COHORT_KEY,
    DatasetImportError,
    import_question_records,
)
from agent_eval.datasets.livebench_builder import (
    LIVEBENCH_CATEGORIES,
    LIVEBENCH_QUOTAS,
    LIVEBENCH_SAMPLING_SEED,
    LiveBenchBuildError,
    build_livebench_category_records,
    build_livebench_dataset,
    select_livebench_rows,
)
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import Ability, Language, QuestionType
from agent_eval.db.models import DatasetVersion, ExecutionItem, Question
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.runs.profiles import (
    DEFAULT_PROFILE_KEY,
    HELDOUT_V1_PROFILE_KEY,
    available_run_profiles,
    build_run_profiles,
    default_profile_key,
    is_complete_heldout_v1,
)


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def livebench_rows(category: str, count: int = 25) -> list[dict[str, object]]:
    release = LIVEBENCH_CATEGORIES[category]["release"]
    rows: list[dict[str, object]] = []
    for index in range(count):
        rows.append(
            {
                "question_id": f"{category}-question-{index:03d}",
                "task": "synthetic-task",
                "turns": [f"LiveBench question {index} for {category}"],
                "ground_truth": f"answer-{index}",
                "livebench_release_date": f"{release}T00:00:00",
                "livebench_removal_date": "",
            }
        )
    return rows


def test_livebench_selection_filters_release_and_quality() -> None:
    release = LIVEBENCH_CATEGORIES["math"]["release"]
    rows = livebench_rows("math")
    rows.extend(
        [
            {  # older release
                "question_id": "old-release",
                "task": "synthetic-task",
                "turns": ["old question"],
                "ground_truth": "answer",
                "livebench_release_date": "2024-06-24T00:00:00",
                "livebench_removal_date": "",
            },
            {  # removed from the live benchmark
                "question_id": "removed",
                "task": "synthetic-task",
                "turns": ["removed question"],
                "ground_truth": "answer",
                "livebench_release_date": f"{release}T00:00:00",
                "livebench_removal_date": "2025-01-01",
            },
            {  # no objective ground truth
                "question_id": "no-answer",
                "task": "synthetic-task",
                "turns": ["open question"],
                "ground_truth": "  ",
                "livebench_release_date": f"{release}T00:00:00",
                "livebench_removal_date": "",
            },
            {  # multi-turn questions are not importable as single prompts
                "question_id": "multi-turn",
                "task": "synthetic-task",
                "turns": ["first", "second"],
                "ground_truth": "answer",
                "livebench_release_date": f"{release}T00:00:00",
                "livebench_removal_date": "",
            },
        ]
    )
    selected = select_livebench_rows(rows, release=release, quota=20)
    assert len(selected) == 20
    assert {row["question_id"] for row in selected} <= {
        f"math-question-{index:03d}" for index in range(25)
    }
    assert selected == select_livebench_rows(rows, release=release, quota=20)
    with pytest.raises(LiveBenchBuildError, match="below the quota"):
        select_livebench_rows(rows, release=release, quota=26)


def test_livebench_category_records_map_to_released_abilities() -> None:
    expected_abilities = {
        "math": Ability.REASONING,
        "reasoning": Ability.REASONING,
        "data_analysis": Ability.DATA_REASONING,
    }
    for category, ability in expected_abilities.items():
        spec = LIVEBENCH_CATEGORIES[category]
        records = build_livebench_category_records(category, livebench_rows(category))
        assert len(records) == LIVEBENCH_QUOTAS[spec["benchmark"]]
        sample = records[0]
        assert sample.benchmark == spec["benchmark"]
        assert sample.ability == ability
        assert sample.language == Language.EN
        assert sample.type == QuestionType.SHORT_ANSWER
        assert sample.choices is None
        assert sample.sampling_seed == LIVEBENCH_SAMPLING_SEED
        assert spec["revision"] in sample.source_version
        assert spec["release"] in sample.source_version
        assert sample.reference_answer.startswith("answer-")
        assert len({record.external_id for record in records}) == len(records)


def test_livebench_dataset_writes_per_benchmark_jsonl_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_eval.datasets.livebench_builder as builder

    output = tmp_path / "livebench"

    def fake_build(category: str, rows: object = None, *, release: str | None = None) -> list:
        return build_livebench_category_records(category, livebench_rows(category))

    monkeypatch.setattr(builder, "build_livebench_category_records", fake_build)
    built = build_livebench_dataset(output)

    assert len(built) == sum(LIVEBENCH_QUOTAS.values())
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cohort_key"] == HELDOUT_V1_COHORT_KEY
    assert manifest["question_count"] == sum(LIVEBENCH_QUOTAS.values())
    for benchmark, quota in LIVEBENCH_QUOTAS.items():
        entry = manifest["benchmarks"][benchmark]
        assert entry["question_count"] == quota
        path = output / entry["file"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == quota


async def seed_heldout_versions(session) -> list:
    records: list = []
    for category in sorted(LIVEBENCH_CATEGORIES):
        records.extend(build_livebench_category_records(category, livebench_rows(category)))
    return await import_question_records(
        session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
    )


async def test_held_out_import_marks_versions_and_rejects_conflicts(tmp_path: Path) -> None:
    database_path = tmp_path / "heldout.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    records: list = []
    for category in sorted(LIVEBENCH_CATEGORIES):
        records.extend(build_livebench_category_records(category, livebench_rows(category)))
    try:
        async with session_factory() as session:
            with pytest.raises(DatasetImportError, match="heldout-v1 cohort"):
                await import_question_records(session, records, held_out=True)
            await session.rollback()
            with pytest.raises(DatasetImportError, match="cohort"):
                await import_question_records(session, records, cohort_key="random-cohort")
            await session.rollback()

        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
            )
        assert all(item.created for item in imported)
        assert len(imported) == 3

        async with session_factory() as session:
            versions = list(await session.scalars(select(DatasetVersion)))
            assert {version.cohort_key for version in versions} == {HELDOUT_V1_COHORT_KEY}
            assert all(version.held_out for version in versions)

        async with session_scope(session_factory) as session:
            reused = await import_question_records(
                session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
            )
        assert not any(item.created for item in reused)

        async with session_factory() as session:
            with pytest.raises(DatasetImportError, match="held_out"):
                await import_question_records(
                    session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=False
                )
            await session.rollback()
            with pytest.raises(DatasetImportError, match="cohort"):
                await import_question_records(
                    session, records, cohort_key=LEGACY_COHORT_KEY
                )
            await session.rollback()
    finally:
        await engine.dispose()


def _dataset(name: str, count: int, held_out: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        benchmark=SimpleNamespace(name=name), question_count=count, held_out=held_out
    )


def test_heldout_v1_completeness_requires_all_livebench_benchmarks() -> None:
    datasets = [
        _dataset("LiveBench-Math", 20),
        _dataset("LiveBench-Reasoning", 20),
        _dataset("LiveBench-DataAnalysis", 20),
    ]
    assert is_complete_heldout_v1(datasets)
    assert not is_complete_heldout_v1(datasets[:-1])
    assert not is_complete_heldout_v1([*datasets[:-1], _dataset("LiveBench-DataAnalysis", 21)])
    assert not is_complete_heldout_v1(
        [*datasets[:-1], _dataset("LiveBench-DataAnalysis", 20, held_out=False)]
    )


async def test_heldout_v1_profile_is_exposed_without_stealing_default(tmp_path: Path) -> None:
    database_path = tmp_path / "heldout-profile.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            await seed_heldout_versions(session)
        async with session_factory() as session:
            profiles = await build_run_profiles(session)
            available = available_run_profiles(profiles)
            default_key = default_profile_key(profiles)

        heldout = next(
            profile for profile in profiles if profile.key == HELDOUT_V1_PROFILE_KEY
        )
        assert heldout.question_count == 60
        assert heldout.benchmark_count == 3
        assert heldout.ranking_eligible
        assert set(heldout.publishes_to) == {"overall", "reasoning", "data_reasoning"}
        assert HELDOUT_V1_PROFILE_KEY in {profile.key for profile in available}
        assert default_key == DEFAULT_PROFILE_KEY
    finally:
        await engine.dispose()


async def test_run_evidence_detail_masks_held_out_questions(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        run_id = await create_run(client, fixture, mutation_headers)
        item_ids, _ = await seed_evidence(app, run_id)
        async with session_scope(app.state.session_factory) as session:
            dataset_version = await session.get(DatasetVersion, fixture.dataset_version_ids[0])
            assert dataset_version is not None
            dataset_version.held_out = True

        masked = await client.get(
            f"/api/v1/evaluation-runs/{run_id}/evidence/{item_ids['reasoning-1']}"
        )
        assert masked.status_code == 200
        detail = masked.json()
        assert detail["held_out"] is True
        assert detail["prompt"] is None
        assert detail["choices"] is None
        assert detail["reference_answer"] is None
        assert "Question one" not in json.dumps(detail)
        # Worker-visible data stays intact: only the API serialization is masked.
        async with app.state.session_factory() as session:
            stored = await session.get(ExecutionItem, item_ids["reasoning-1"])
            assert stored is not None
            question = await session.get(Question, stored.question_id)
            assert question is not None and question.prompt == "Question one"

        visible = await client.get(
            f"/api/v1/evaluation-runs/{run_id}/evidence/{item_ids['retrieval-1']}"
        )
        assert visible.status_code == 200
        visible_detail = visible.json()
        assert visible_detail["held_out"] is False
        assert visible_detail["prompt"] == "Find the answer"
        assert visible_detail["reference_answer"] == "Answer"


async def test_quality_endpoints_mask_held_out_questions(tmp_path: Path) -> None:
    async with quality_client(tmp_path) as (client, app):
        mutation_headers = await quality_login(client)
        finding_id, _ = await seed_quality_finding(app)

        visible = await client.get(f"/api/v1/quality/findings/{finding_id}")
        assert visible.status_code == 200
        assert visible.json()["held_out"] is False
        assert visible.json()["prompt"] == "请总结系统规则"

        async with session_scope(app.state.session_factory) as session:
            dataset_version = await session.scalar(select(DatasetVersion))
            assert dataset_version is not None
            dataset_version.held_out = True

        masked = await client.get(f"/api/v1/quality/findings/{finding_id}")
        assert masked.status_code == 200
        detail = masked.json()
        assert detail["held_out"] is True
        assert detail["prompt"] is None
        assert detail["reference_answer"] is None
        assert "请总结系统规则" not in json.dumps(detail, ensure_ascii=False)

        confirmed = await client.patch(
            f"/api/v1/quality/findings/{finding_id}",
            headers=mutation_headers,
            json={"status": "confirmed", "note": "确认底线问题"},
        )
        assert confirmed.status_code == 200
        exported = await client.get("/api/v1/quality/feedback-export")
        assert exported.status_code == 200
        feedback = json.loads(exported.text)
        assert feedback["question"]["held_out"] is True
        assert feedback["question"]["prompt"] is None
        assert feedback["question"]["reference_answer"] is None
        assert "请总结系统规则" not in exported.text


def test_release_response_masks_cases_formalized_into_held_out_versions() -> None:
    from datetime import UTC, datetime

    from agent_eval.api.routes.dataset_releases import release_response
    from agent_eval.db.enums import DatasetReleaseStatus

    now = datetime.now(UTC)
    case_id = UUID("11111111-1111-1111-1111-111111111111")

    def case_link(position: int, held_out: bool | None) -> SimpleNamespace:
        formal_question = (
            None
            if held_out is None
            else SimpleNamespace(dataset_version=SimpleNamespace(held_out=held_out))
        )
        return SimpleNamespace(
            position=position,
            record_hash="a" * 64,
            evaluation_case=SimpleNamespace(
                id=case_id,
                prompt="curated case prompt",
                language=Language.ZH,
                ability=Ability.REASONING,
                question_type=QuestionType.SHORT_ANSWER,
                formal_question=formal_question,
            ),
        )

    release = SimpleNamespace(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        name="curated release",
        description=None,
        status=DatasetReleaseStatus.DRAFT,
        revision=0,
        manifest_hash=None,
        approved_by_user_id=None,
        approved_at=None,
        published_by_user_id=None,
        published_at=None,
        created_at=now,
        updated_at=now,
        case_links=[case_link(1, True), case_link(2, False), case_link(3, None)],
        dataset_versions=[],
    )
    response = release_response(release)
    assert [item.prompt for item in response.cases] == [
        None,
        "curated case prompt",
        "curated case prompt",
    ]
    assert [item.held_out for item in response.cases] == [True, False, False]
