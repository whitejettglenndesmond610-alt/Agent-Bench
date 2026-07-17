import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_eval.db.enums import Ability, Language, QuestionType

DEMO_DISTRIBUTION = {
    ("C-Eval", Language.ZH): 30,
    ("MMLU-Pro", Language.EN): 10,
    ("BrowseComp-ZH", Language.ZH): 20,
    ("BrowseComp", Language.EN): 10,
    ("BizFinBench", Language.ZH): 20,
    ("FinQA", Language.EN): 10,
}


class DatasetValidationError(ValueError):
    pass


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=10)
    text: str = Field(min_length=1)

    @field_validator("label", "text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class QuestionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    benchmark: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1)
    source_version: str = Field(min_length=1, max_length=120)
    license_note: str | None = None
    sampling_seed: int
    external_id: str = Field(min_length=1, max_length=200)
    language: Language
    ability: Ability
    type: QuestionType
    prompt: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    choices: list[Choice] | None = None
    requires_citations: bool = False
    difficulty: str | None = Field(default=None, max_length=80)
    domain: str | None = Field(default=None, max_length=120)

    @field_validator(
        "benchmark", "source", "source_version", "external_id", "prompt", "reference_answer"
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("license_note", "difficulty", "domain")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_question_shape(self) -> Self:
        if self.type == QuestionType.MULTIPLE_CHOICE:
            if self.choices is None or len(self.choices) < 2:
                raise ValueError("multiple-choice questions require at least two choices")
            labels = [choice.label for choice in self.choices]
            if len(labels) != len(set(labels)):
                raise ValueError("choice labels must be unique")
            if self.reference_answer not in labels:
                raise ValueError("reference answer must match a choice label")
        elif self.choices is not None:
            raise ValueError("choices are only valid for multiple-choice questions")

        if self.ability == Ability.RETRIEVAL and not self.requires_citations:
            raise ValueError("retrieval questions must require citations")
        return self


def load_question_records(path: Path) -> list[QuestionRecord]:
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    if not files or any(not file.is_file() for file in files):
        raise DatasetValidationError(f"dataset path contains no JSONL files: {path}")

    records: list[QuestionRecord] = []
    for file in files:
        with file.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(QuestionRecord.model_validate_json(line))
                except (ValueError, json.JSONDecodeError) as error:
                    raise DatasetValidationError(f"{file}:{line_number}: {error}") from error

    if not records:
        raise DatasetValidationError("dataset contains no questions")
    validate_unique_records(records)
    return records


def validate_unique_records(records: list[QuestionRecord]) -> None:
    seen: set[tuple[str, Language, str]] = set()
    benchmark_metadata: dict[tuple[str, Language], tuple[Ability, str, str, int]] = {}
    for record in records:
        identity = (record.benchmark, record.language, record.external_id)
        if identity in seen:
            raise DatasetValidationError(
                f"duplicate external_id {record.external_id!r} in {record.benchmark}"
            )
        seen.add(identity)

        benchmark_key = (record.benchmark, record.language)
        metadata = (record.ability, record.source, record.source_version, record.sampling_seed)
        previous = benchmark_metadata.setdefault(benchmark_key, metadata)
        if previous != metadata:
            raise DatasetValidationError(
                f"inconsistent benchmark metadata for {record.benchmark}/{record.language}"
            )


def validate_demo_distribution(records: list[QuestionRecord]) -> None:
    distribution = Counter((record.benchmark, record.language) for record in records)
    if distribution != Counter(DEMO_DISTRIBUTION):
        expected = {
            f"{name}/{language}": count for (name, language), count in DEMO_DISTRIBUTION.items()
        }
        actual = {f"{name}/{language}": count for (name, language), count in distribution.items()}
        raise DatasetValidationError(
            f"demo benchmark distribution mismatch; expected {expected}, got {actual}"
        )

    language_counts = Counter(record.language for record in records)
    if language_counts != Counter({Language.ZH: 70, Language.EN: 30}):
        raise DatasetValidationError("demo language distribution must be zh=70 and en=30")

    ability_counts = Counter(record.ability for record in records)
    expected_abilities = Counter(
        {Ability.REASONING: 40, Ability.RETRIEVAL: 30, Ability.DATA_REASONING: 30}
    )
    if ability_counts != expected_abilities:
        raise DatasetValidationError(
            "demo ability distribution must be reasoning=40, retrieval=30, data=30"
        )

    question_types = {record.type for record in records}
    if question_types != set(QuestionType):
        raise DatasetValidationError("demo dataset must contain all three question types")

    bizfin_types = {record.type for record in records if record.benchmark == "BizFinBench"}
    if not {QuestionType.SHORT_ANSWER, QuestionType.OPEN_ENDED} <= bizfin_types:
        raise DatasetValidationError(
            "BizFinBench must contain short-answer and open-ended questions"
        )


def records_content_hash(records: list[QuestionRecord]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item.external_id):
        payload = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
