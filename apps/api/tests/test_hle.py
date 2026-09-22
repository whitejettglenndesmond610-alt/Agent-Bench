import asyncio
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from agent_eval.core.settings import Settings
from agent_eval.datasets.hle_builder import (
    HLE_BENCHMARK,
    HLE_LICENSE,
    HLE_QUOTA,
    HLE_REVISION,
    HLE_SAMPLING_SEED,
    HleBuildError,
    build_hle_dataset,
    build_hle_records,
    hle_record,
    parse_answer_choices,
    resolve_hf_token,
    select_hle_rows,
)
from agent_eval.datasets.importer import (
    HELDOUT_V1_COHORT_KEY,
    LEGACY_COHORT_KEY,
    DatasetImportError,
    import_question_records,
)
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import Ability, Language, QuestionType
from agent_eval.db.models import DatasetVersion
from agent_eval.db.session import create_session_factory, session_scope


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


MC_QUESTION = (
    "Which condition of Arrhenius's sixth impossibility theorem do critical-level "
    "views violate?\n\nAnswer Choices:\nA. Egalitarian Dominance\n"
    "B. General Non-Extreme Priority\nC. Non-Elitism\nD. Weak Non-Sadism"
)

MULTILINE_MC_QUESTION = (
    "Which function satisfies this property?\n\nAnswer Choices:\nA. 4\n"
    "B. \\[\n\\frac{L^2}{d}\n\\]\nC. $T_{3}(x) = \\log{(1 + e^{x})}$\nD. none"
)


def hle_rows(count: int = 45) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        multiple_choice = index % 4 == 0
        rows.append(
            {
                "id": f"hle-{index:04d}",
                "question": (
                    f"{MC_QUESTION} (variant {index})"
                    if multiple_choice
                    else f"Compute the exact answer for variant {index}."
                ),
                "image": "",
                "answer": "B" if multiple_choice else f"answer-{index}",
                "answer_type": "multipleChoice" if multiple_choice else "exactMatch",
                "category": "Math",
            }
        )
    return rows


def test_parse_answer_choices_handles_multiline_options() -> None:
    parsed = parse_answer_choices(MULTILINE_MC_QUESTION)
    assert parsed is not None
    stem, choices = parsed
    assert stem == "Which function satisfies this property?"
    assert [choice.label for choice in choices] == ["A", "B", "C", "D"]
    assert choices[1].text == "\\[\n\\frac{L^2}{d}\n\\]"
    assert parse_answer_choices("No choices here") is None
    assert parse_answer_choices("Answer Choices:\nA. only one") is None
    assert parse_answer_choices("Answer Choices:\nB. wrong start\nC. order") is None


def test_hle_selection_filters_to_text_only_objective_questions() -> None:
    rows = hle_rows()
    rows.extend(
        [
            {  # multi-modal question
                "id": "hle-image",
                "question": "Describe the image.",
                "image": "https://example.test/image.png",
                "answer": "answer",
                "answer_type": "exactMatch",
                "category": "Math",
            },
            {  # unknown future answer type
                "id": "hle-open",
                "question": "Write an essay.",
                "image": "",
                "answer": "rubric",
                "answer_type": "modelAnswer",
                "category": "Other",
            },
            {  # empty answer
                "id": "hle-empty",
                "question": "Unanswerable?",
                "image": "",
                "answer": "  ",
                "answer_type": "exactMatch",
                "category": "Math",
            },
        ]
    )
    selected = select_hle_rows(rows)
    assert len(selected) == HLE_QUOTA
    assert {row["id"] for row in selected} <= {f"hle-{index:04d}" for index in range(45)}
    assert selected == select_hle_rows(rows)
    with pytest.raises(HleBuildError, match="below the quota"):
        select_hle_rows(rows[:20])


def test_hle_records_map_choice_and_exact_match_questions() -> None:
    records = build_hle_records(hle_rows())
    assert len(records) == HLE_QUOTA
    multiple_choice = [record for record in records if record.type == QuestionType.MULTIPLE_CHOICE]
    short_answer = [record for record in records if record.type == QuestionType.SHORT_ANSWER]
    assert multiple_choice and short_answer
    for record in records:
        assert record.benchmark == HLE_BENCHMARK
        assert record.ability == Ability.REASONING
        assert record.language == Language.EN
        assert record.source_version == HLE_REVISION
        assert record.sampling_seed == HLE_SAMPLING_SEED
        assert record.license_note == HLE_LICENSE
        assert record.domain == "Math"
    sample = multiple_choice[0]
    assert sample.choices is not None and len(sample.choices) == 4
    assert sample.reference_answer == "B"
    assert "Answer Choices:" not in sample.prompt
    for record in short_answer:
        assert record.choices is None


def test_hle_record_falls_back_to_short_answer_when_choices_unparseable() -> None:
    row = {
        "id": "hle-broken",
        "question": "Pick one. Options are listed inline, not after the marker.",
        "image": "",
        "answer": "A",
        "answer_type": "multipleChoice",
        "category": "Other",
    }
    record = hle_record(row)
    assert record.type == QuestionType.SHORT_ANSWER
    assert record.choices is None
    assert record.reference_answer == "A"


def test_hle_token_resolution_requires_environment() -> None:
    assert resolve_hf_token({"HF_TOKEN": " hf-test "}) == "hf-test"
    assert resolve_hf_token({"HUGGING_FACE_HUB_TOKEN": "hub-test"}) == "hub-test"
    assert (
        resolve_hf_token({"HF_TOKEN": "first", "HUGGING_FACE_HUB_TOKEN": "second"}) == "first"
    )
    with pytest.raises(HleBuildError, match="HF_TOKEN"):
        resolve_hf_token({})
    with pytest.raises(HleBuildError, match="HF_TOKEN"):
        resolve_hf_token({"HF_TOKEN": "  "})


def test_build_hle_dataset_writes_jsonl_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_eval.datasets.hle_builder as builder

    monkeypatch.setattr(builder, "build_hle_records", lambda: build_hle_records(hle_rows()))
    output = tmp_path / "hle"
    built = build_hle_dataset(output)
    assert len(built) == HLE_QUOTA
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cohort_key"] == HELDOUT_V1_COHORT_KEY
    entry = manifest["benchmarks"][HLE_BENCHMARK]
    assert entry["question_count"] == HLE_QUOTA
    lines = (output / entry["file"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == HLE_QUOTA


async def test_hle_import_marks_held_out_and_rejects_conflicts(tmp_path: Path) -> None:
    database_path = tmp_path / "hle-heldout.db"
    await asyncio.to_thread(command.upgrade, migration_config(database_path), "head")
    engine = create_engine(
        Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    )
    session_factory = create_session_factory(engine)
    records = build_hle_records(hle_rows())
    try:
        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
            )
        assert len(imported) == 1 and imported[0].created
        async with session_factory() as session:
            version = await session.scalar(select(DatasetVersion))
            assert version is not None and version.held_out
            assert version.cohort_key == HELDOUT_V1_COHORT_KEY
            assert version.question_count == HLE_QUOTA

        async with session_scope(session_factory) as session:
            reused = await import_question_records(
                session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
            )
        assert not reused[0].created

        async with session_factory() as session:
            with pytest.raises(DatasetImportError, match="cohort"):
                await import_question_records(session, records, cohort_key=LEGACY_COHORT_KEY)
            await session.rollback()
            with pytest.raises(DatasetImportError, match="held_out"):
                await import_question_records(
                    session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=False
                )
            await session.rollback()
    finally:
        await engine.dispose()
