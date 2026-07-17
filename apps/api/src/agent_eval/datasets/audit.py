import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import Any, Literal

from agent_eval.datasets.schema import (
    QuestionRecord,
    records_content_hash,
    validate_unique_records,
)

type AuditPolicy = Literal["balanced-v2"]

BALANCED_V2_DIMENSIONS = (
    "reasoning",
    "retrieval",
    "data_reasoning",
    "instruction_following",
    "long_context",
)
BALANCED_V2_QUESTIONS_PER_DIMENSION = 30
BALANCED_V2_LANGUAGE_QUOTA = {"zh": 15, "en": 15}
BALANCED_V2_DIFFICULTY_QUOTA = {"easy": 6, "medium": 18, "hard": 6}
CANONICAL_DIFFICULTIES = frozenset(BALANCED_V2_DIFFICULTY_QUOTA)


def audit_question_records(
    records: list[QuestionRecord], *, policy: AuditPolicy | None = None
) -> dict[str, Any]:
    validate_unique_records(records)
    benchmark_groups: dict[tuple[str, str], list[QuestionRecord]] = defaultdict(list)
    for record in records:
        benchmark_groups[(record.benchmark, record.language.value)].append(record)

    benchmark_summaries: list[dict[str, Any]] = []
    bundle_entries: list[dict[str, Any]] = []
    for (benchmark, language), group in sorted(benchmark_groups.items()):
        content_hash = records_content_hash(group)
        summary: dict[str, Any] = {
            "benchmark": benchmark,
            "language": language,
            "ability": group[0].ability.value,
            "question_count": len(group),
            "source": group[0].source,
            "source_version": group[0].source_version,
            "sampling_seed": group[0].sampling_seed,
            "content_hash": content_hash,
        }
        benchmark_summaries.append(summary)
        bundle_entries.append(
            {
                "benchmark": benchmark,
                "language": language,
                "content_hash": content_hash,
            }
        )

    serialized_bundle = json.dumps(
        bundle_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    report: dict[str, Any] = {
        "report_version": "1.0",
        "policy": policy,
        "question_count": len(records),
        "canonical_bundle_hash": hashlib.sha256(serialized_bundle.encode()).hexdigest(),
        "benchmarks": benchmark_summaries,
        "distributions": {
            "ability": _counts(record.ability.value for record in records),
            "language": _counts(record.language.value for record in records),
            "question_type": _counts(record.type.value for record in records),
            "requires_citations": _counts(
                "true" if record.requires_citations else "false" for record in records
            ),
            "difficulty": _counts(record.difficulty or "missing" for record in records),
            "domain": _counts(record.domain or "missing" for record in records),
            "ability_by_language": _cross_counts(
                records,
                lambda record: record.ability.value,
                lambda record: record.language.value,
            ),
            "ability_by_question_type": _cross_counts(
                records,
                lambda record: record.ability.value,
                lambda record: record.type.value,
            ),
            "ability_by_difficulty": _cross_counts(
                records,
                lambda record: record.ability.value,
                lambda record: record.difficulty or "missing",
            ),
        },
        "metadata_coverage": {
            "difficulty": _coverage(record.difficulty is not None for record in records),
            "domain": _coverage(record.domain is not None for record in records),
            "license_note": _coverage(record.license_note is not None for record in records),
        },
        "prompt_characters": _length_summary([len(record.prompt) for record in records]),
        "reference_answer_characters": _length_summary(
            [len(record.reference_answer) for record in records]
        ),
        "violations": [],
    }
    if policy == "balanced-v2":
        report["policy_target"] = _balanced_v2_target()
        report["violations"] = _balanced_v2_violations(records)
        report["policy_passed"] = not report["violations"]
    return report


def render_audit_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _counts(values: Iterable[str]) -> dict[str, int]:
    return {key: count for key, count in sorted(Counter(values).items())}


def _cross_counts(
    records: list[QuestionRecord],
    outer: Callable[[QuestionRecord], str],
    inner: Callable[[QuestionRecord], str],
) -> dict[str, dict[str, int]]:
    rows: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        rows[outer(record)][inner(record)] += 1
    return {
        outer_key: {inner_key: count for inner_key, count in sorted(counts.items())}
        for outer_key, counts in sorted(rows.items())
    }


def _coverage(values: Iterable[bool]) -> dict[str, int | float]:
    rows = list(values)
    present = sum(rows)
    total = len(rows)
    return {
        "present": present,
        "missing": total - present,
        "percent": round(present * 100 / total, 2) if total else 0.0,
    }


def _length_summary(values: list[int]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {
            "min": 0,
            "p50": 0,
            "p95": 0,
            "max": 0,
            "buckets": {"short_0_1000": 0, "medium_1001_8000": 0, "long_8001_plus": 0},
        }
    return {
        "min": ordered[0],
        "p50": _percentile(ordered, 50),
        "p95": _percentile(ordered, 95),
        "max": ordered[-1],
        "buckets": {
            "short_0_1000": sum(value <= 1000 for value in ordered),
            "medium_1001_8000": sum(1000 < value <= 8000 for value in ordered),
            "long_8001_plus": sum(value > 8000 for value in ordered),
        },
    }


def _percentile(ordered: list[int], percentile: int) -> int:
    index = max(0, (len(ordered) * percentile + 99) // 100 - 1)
    return ordered[index]


def _balanced_v2_target() -> dict[str, Any]:
    return {
        "question_count": len(BALANCED_V2_DIMENSIONS) * BALANCED_V2_QUESTIONS_PER_DIMENSION,
        "dimensions": {
            dimension: {
                "question_count": BALANCED_V2_QUESTIONS_PER_DIMENSION,
                "language": dict(BALANCED_V2_LANGUAGE_QUOTA),
                "difficulty": dict(BALANCED_V2_DIFFICULTY_QUOTA),
            }
            for dimension in BALANCED_V2_DIMENSIONS
        },
        "canonical_difficulties": sorted(CANONICAL_DIFFICULTIES),
        "required_metadata": ["difficulty", "domain", "license_note"],
    }


def _balanced_v2_violations(records: list[QuestionRecord]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    ability_counts = Counter(record.ability.value for record in records)
    language_counts = Counter(
        (record.ability.value, record.language.value) for record in records
    )
    difficulty_counts = Counter(
        (record.ability.value, record.difficulty) for record in records
    )

    expected_total = len(BALANCED_V2_DIMENSIONS) * BALANCED_V2_QUESTIONS_PER_DIMENSION
    if len(records) != expected_total:
        violations.append(
            _violation("question_count", expected_total, len(records))
        )
    for dimension in BALANCED_V2_DIMENSIONS:
        actual_dimension = ability_counts[dimension]
        if actual_dimension != BALANCED_V2_QUESTIONS_PER_DIMENSION:
            violations.append(
                _violation(
                    "dimension_count",
                    BALANCED_V2_QUESTIONS_PER_DIMENSION,
                    actual_dimension,
                    dimension=dimension,
                )
            )
        for language, expected in BALANCED_V2_LANGUAGE_QUOTA.items():
            actual = language_counts[(dimension, language)]
            if actual != expected:
                violations.append(
                    _violation(
                        "dimension_language_count",
                        expected,
                        actual,
                        dimension=dimension,
                        slice_name=language,
                    )
                )
        for difficulty, expected in BALANCED_V2_DIFFICULTY_QUOTA.items():
            actual = difficulty_counts[(dimension, difficulty)]
            if actual != expected:
                violations.append(
                    _violation(
                        "dimension_difficulty_count",
                        expected,
                        actual,
                        dimension=dimension,
                        slice_name=difficulty,
                    )
                )

    missing_difficulty = sum(record.difficulty is None for record in records)
    if missing_difficulty:
        violations.append(_violation("difficulty_missing", 0, missing_difficulty))
    unknown_difficulties = Counter(
        record.difficulty
        for record in records
        if record.difficulty is not None and record.difficulty not in CANONICAL_DIFFICULTIES
    )
    for difficulty, count in sorted(unknown_difficulties.items()):
        violations.append(
            _violation("difficulty_unknown", 0, count, slice_name=difficulty)
        )
    for field, missing in (
        ("domain", sum(record.domain is None for record in records)),
        ("license_note", sum(record.license_note is None for record in records)),
    ):
        if missing:
            violations.append(
                _violation(f"{field}_missing", 0, missing)
            )
    return sorted(
        violations,
        key=lambda item: (
            str(item["code"]),
            str(item.get("dimension", "")),
            str(item.get("slice", "")),
        ),
    )


def _violation(
    code: str,
    expected: int,
    actual: int,
    *,
    dimension: str | None = None,
    slice_name: str | None = None,
) -> dict[str, Any]:
    violation: dict[str, Any] = {
        "code": code,
        "expected": expected,
        "actual": actual,
        "difference": actual - expected,
    }
    if dimension is not None:
        violation["dimension"] = dimension
    if slice_name is not None:
        violation["slice"] = slice_name
    return violation
