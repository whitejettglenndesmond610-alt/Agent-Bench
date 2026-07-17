import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, TypedDict

from agent_eval.datasets.audit import audit_question_records
from agent_eval.datasets.schema import (
    Choice,
    DatasetValidationError,
    QuestionRecord,
    load_question_records,
)
from agent_eval.db.enums import Ability, Language, QuestionType

SAMPLING_SEED = 20260717
ORIGINAL_SOURCE_VERSION = "balanced-v2-original-2026-07-17"
ORIGINAL_LICENSE = "Original synthetic evaluation material; internal redistribution approved"
ANCHOR_QUOTAS = {
    ("C-Eval", Language.ZH): 15,
    ("MMLU-Pro", Language.EN): 10,
    ("BrowseComp-ZH", Language.ZH): 15,
    ("BrowseComp", Language.EN): 10,
    ("BizFinBench", Language.ZH): 15,
    ("FinQA", Language.EN): 10,
}
DOMAIN_TAXONOMY = {
    Ability.REASONING: ("mathematics", "science", "humanities", "social_science", "professional"),
    Ability.RETRIEVAL: ("standards", "science", "culture", "geography", "business"),
    Ability.DATA_REASONING: (
        "financial_table",
        "financial_text",
        "statistics",
        "comparison",
        "calculation",
    ),
    Ability.INSTRUCTION_FOLLOWING: (
        "exact_output",
        "formatting",
        "transformation",
        "filtering",
        "constraint_satisfaction",
    ),
    Ability.LONG_CONTEXT: (
        "single_fact",
        "cross_reference",
        "aggregation",
        "temporal_reasoning",
        "record_comparison",
    ),
}
SOURCE_REVIEWS: list[dict[str, str]] = [
    {
        "source": "C-Eval",
        "decision": "internal_research_only",
        "reason": "CC BY-NC-SA 4.0; attribution and noncommercial restrictions retained",
    },
    {
        "source": "MMLU-Pro",
        "decision": "approved_with_attribution",
        "reason": "MIT dataset release; source attribution retained",
    },
    {
        "source": "BrowseComp and BrowseComp-ZH",
        "decision": "local_only",
        "reason": "decrypted examples must not be redistributed and ZH license metadata conflicts",
    },
    {
        "source": "BizFinBench",
        "decision": "internal_research_only",
        "reason": "CC BY-NC 4.0; attribution and noncommercial restrictions retained",
    },
    {
        "source": "FinQA",
        "decision": "approved_with_attribution",
        "reason": "CC BY 4.0; attribution and modification notice retained",
    },
    {
        "source": "IFEval",
        "decision": "not_selected",
        "reason": "Apache-2.0 repository, but prompts require dedicated rule verifiers",
    },
    {
        "source": "LongBench v2",
        "decision": "not_selected",
        "reason": "MIT repository, but upstream context redistribution rights are heterogeneous",
    },
    {
        "source": "Balanced V2 original synthetic tasks",
        "decision": "approved_internal_original",
        "reason": (
            "programmatically generated without third-party text and deterministically verified"
        ),
    },
]


class InstructionRow(TypedDict):
    id: str
    score: int


def build_balanced_v2_dataset(anchor_path: Path, output: Path) -> dict[str, Any]:
    anchors = _select_anchors(load_question_records(anchor_path))
    originals = [
        *_original_reasoning(),
        *_original_retrieval(),
        *_original_data_reasoning(),
        *_instruction_following(),
        *_long_context(),
    ]
    records = _normalize_metadata([*anchors, *originals])
    audit = audit_question_records(records, policy="balanced-v2")
    quality = validate_balanced_v2_quality(records, audit)

    output.mkdir(parents=True, exist_ok=True)
    for stale in output.glob("*.jsonl"):
        stale.unlink()
    files: list[dict[str, Any]] = []
    by_benchmark: dict[str, list[QuestionRecord]] = defaultdict(list)
    for record in records:
        by_benchmark[record.benchmark].append(record)
    for benchmark, group in sorted(by_benchmark.items()):
        path = output / f"{_slug(benchmark)}.jsonl"
        content = "".join(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for record in sorted(group, key=lambda item: (item.language.value, item.external_id))
        )
        path.write_text(content, encoding="utf-8")
        files.append(
            {
                "file": path.name,
                "question_count": len(group),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )

    manifest = {
        "schema_version": "1.0",
        "policy": "balanced-v2",
        "sampling_seed": SAMPLING_SEED,
        "question_count": len(records),
        "canonical_bundle_hash": audit["canonical_bundle_hash"],
        "release_scope": "internal_research_only",
        "redistributable": False,
        "files": files,
        "source_reviews": SOURCE_REVIEWS,
        "quality": quality,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def validate_balanced_v2_quality(
    records: list[QuestionRecord], audit: dict[str, Any] | None = None
) -> dict[str, Any]:
    report = audit or audit_question_records(records, policy="balanced-v2")
    if not report["policy_passed"]:
        raise DatasetValidationError(
            f"balanced-v2 policy has {len(report['violations'])} violations"
        )
    prompt_hashes = [
        hashlib.sha256(" ".join(record.prompt.split()).casefold().encode()).hexdigest()
        for record in records
    ]
    if len(prompt_hashes) != len(set(prompt_hashes)):
        raise DatasetValidationError("balanced-v2 contains duplicate normalized prompts")
    long_context = [record for record in records if record.ability == Ability.LONG_CONTEXT]
    if len(long_context) != 30 or min(map(lambda record: len(record.prompt), long_context)) <= 8000:
        raise DatasetValidationError("all long-context prompts must exceed 8000 characters")
    instruction = [
        record for record in records if record.ability == Ability.INSTRUCTION_FOLLOWING
    ]
    if len(instruction) != 30 or any(
        record.type != QuestionType.SHORT_ANSWER for record in instruction
    ):
        raise DatasetValidationError(
            "instruction-following cohort must contain 30 exact-answer tasks"
        )
    retrieval_originals = [
        record
        for record in records
        if record.benchmark == "BalancedV2-Original-Retrieval"
    ]
    if len(retrieval_originals) != 5 or any(
        "https://" not in item.prompt for item in retrieval_originals
    ):
        raise DatasetValidationError(
            "original retrieval tasks must identify stable HTTPS sources"
        )
    original_count = sum(record.source.startswith("internal://") for record in records)
    return {
        "status": "automated_pass",
        "schema_validated": len(records),
        "official_reference_anchors": len(records) - original_count,
        "deterministic_original_answers": original_count,
        "normalized_prompt_duplicates": 0,
        "long_context_min_characters": min(len(record.prompt) for record in long_context),
        "human_release_review_required": True,
    }


def _select_anchors(records: list[QuestionRecord]) -> list[QuestionRecord]:
    selected: list[QuestionRecord] = []
    for key, quota in ANCHOR_QUOTAS.items():
        candidates = [record for record in records if (record.benchmark, record.language) == key]
        if len(candidates) < quota:
            raise DatasetValidationError(f"not enough anchor questions for {key[0]}/{key[1].value}")
        selected.extend(sorted(candidates, key=_stable_record_key)[:quota])
    if len(selected) != 75:
        raise DatasetValidationError("balanced-v2 anchor selection must contain 75 questions")
    return selected


def _normalize_metadata(records: list[QuestionRecord]) -> list[QuestionRecord]:
    normalized: list[QuestionRecord] = []
    groups: dict[tuple[Ability, Language], list[QuestionRecord]] = defaultdict(list)
    for record in records:
        groups[(record.ability, record.language)].append(record)
    for (ability, language), group in sorted(
        groups.items(), key=lambda item: (item[0][0].value, item[0][1].value)
    ):
        if len(group) != 15:
            raise DatasetValidationError(
                f"balanced-v2 requires 15 questions for {ability.value}/{language.value}"
            )
        domains = DOMAIN_TAXONOMY[ability]
        ordered = sorted(group, key=_stable_record_key)
        for index, record in enumerate(ordered):
            difficulty = "easy" if index < 3 else "medium" if index < 12 else "hard"
            normalized.append(
                record.model_copy(
                    update={
                        "sampling_seed": SAMPLING_SEED,
                        "difficulty": difficulty,
                        "domain": domains[index % len(domains)],
                    }
                )
            )
    return normalized


def _original_reasoning() -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for index in range(5):
        start = 7 + index * 3
        added = 4 + index
        multiplier = 2 + index % 2
        subtracted = 3 + index
        answer = (start + added) * multiplier - subtracted
        values = [answer + 1, answer - multiplier, answer + added, answer]
        correct_index = index % 4
        values[correct_index], values[-1] = values[-1], values[correct_index]
        labels = ["A", "B", "C", "D"]
        records.append(
            _original_record(
                benchmark="BalancedV2-Original-Reasoning",
                external_id=f"reasoning-en-{index + 1:02d}",
                language=Language.EN,
                ability=Ability.REASONING,
                question_type=QuestionType.MULTIPLE_CHOICE,
                prompt=(
                    f"Start with {start}. Add {added}, multiply the result by {multiplier}, "
                    f"then subtract {subtracted}. What is the final value?"
                ),
                reference_answer=labels[correct_index],
                choices=[
                    Choice(label=label, text=str(value))
                    for label, value in zip(labels, values, strict=True)
                ],
            )
        )
    return records


def _original_retrieval() -> list[QuestionRecord]:
    tasks = [
        (
            "https://www.rfc-editor.org/rfc/rfc9110.html",
            "According to the RFC Editor page for RFC 9110, in which month and year "
            "was it published?",
            "June 2022",
        ),
        (
            "https://docs.python.org/3.12/library/json.html",
            "According to the Python json module documentation, which exception is raised "
            "when a JSON document is invalid?",
            "json.JSONDecodeError",
        ),
        (
            "https://www.sqlite.org/wal.html",
            "According to SQLite's WAL documentation, what does WAL stand for?",
            "Write-Ahead Logging",
        ),
        (
            "https://www.w3.org/TR/2024/REC-WCAG22-20241212/",
            "According to this versioned W3C Recommendation page, on what date was WCAG 2.2 "
            "published as a Recommendation?",
            "12 December 2024",
        ),
        (
            "https://www.rfc-editor.org/rfc/rfc9110.html#status.418",
            "According to RFC 9110 section 15.5.19, what parenthesized description is "
            "assigned to status code 418?",
            "(Unused)",
        ),
    ]
    return [
        _original_record(
            benchmark="BalancedV2-Original-Retrieval",
            external_id=f"retrieval-en-{index + 1:02d}",
            language=Language.EN,
            ability=Ability.RETRIEVAL,
            question_type=QuestionType.SHORT_ANSWER,
            prompt=f"Source URL: {url}\n{question} Cite the source URL in the answer.",
            reference_answer=answer,
            requires_citations=True,
        )
        for index, (url, question, answer) in enumerate(tasks)
    ]


def _original_data_reasoning() -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for index in range(5):
        q1 = 120 + index * 20
        q2 = q1 + 30 + index * 5
        q3 = q2 - 10 + index * 3
        average = (q1 + q2 + q3) / 3
        answer = f"{average:.2f}"
        records.append(
            _original_record(
                benchmark="BalancedV2-Original-Data",
                external_id=f"data-en-{index + 1:02d}",
                language=Language.EN,
                ability=Ability.DATA_REASONING,
                question_type=QuestionType.SHORT_ANSWER,
                prompt=(
                    "A synthetic operations table reports quarterly units: "
                    f"Q1={q1}, Q2={q2}, Q3={q3}. What is the arithmetic mean? "
                    "Round to two decimal places."
                ),
                reference_answer=answer,
            )
        )
    return records


def _instruction_following() -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for language in Language:
        for index in range(15):
            if index < 3:
                token = f"V2-{language.value.upper()}-{index + 1}"
                prompt = (
                    f"只输出标记 {token}，不要添加空格、引号或解释。"
                    if language == Language.ZH
                    else f"Output only the token {token}. Add no spaces, quotes, or explanation."
                )
                answer = token
            elif index < 12:
                values = [index + 7, index - 1, index + 2, index + 8, index]
                selected = sorted(value for value in values if value % 2 == 0)
                answer = "|".join(map(str, selected))
                prompt = (
                    f"给定整数 {values}。仅保留偶数，升序排列，并使用竖线连接；只输出结果。"
                    if language == Language.ZH
                    else (
                        f"Given integers {values}, keep only even values, sort ascending, "
                        "join with |, and output only the result."
                    )
                )
            else:
                rows: list[InstructionRow] = [
                    {"id": f"R{item}", "score": (item * 7 + index) % 20}
                    for item in range(1, 6)
                ]
                selected_rows = sorted(
                    (row for row in rows if row["score"] >= 10),
                    key=lambda row: (-row["score"], row["id"]),
                )
                answer = json.dumps(selected_rows, separators=(",", ":"))
                prompt = (
                    f"记录为 {json.dumps(rows, ensure_ascii=False, separators=(',', ':'))}。"
                    "保留 score>=10 的记录，按 score 降序、id 升序排列，"
                    "只输出包含 id 和 score 的紧凑 JSON 数组。"
                    if language == Language.ZH
                    else (
                        f"Records: {json.dumps(rows, separators=(',', ':'))}. Keep score>=10, "
                        "sort by score descending then id ascending, and output only a compact "
                        "JSON array containing id and score."
                    )
                )
            records.append(
                _original_record(
                    benchmark="BalancedV2-Original-Instruction-Following",
                    external_id=f"instruction-{language.value}-{index + 1:02d}",
                    language=language,
                    ability=Ability.INSTRUCTION_FOLLOWING,
                    question_type=QuestionType.SHORT_ANSWER,
                    prompt=prompt,
                    reference_answer=answer,
                )
            )
    return records


def _long_context() -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for language in Language:
        for index in range(15):
            rows = []
            values: dict[int, int] = {}
            for row_index in range(220):
                value = (row_index * 17 + index * 13 + (3 if language == Language.ZH else 7)) % 997
                values[row_index] = value
                if language == Language.ZH:
                    rows.append(
                        f"档案记录 {row_index:03d}：合成站点 S{row_index:03d} "
                        f"的指标为 {value:03d}；"
                        "该记录仅用于内部长上下文评测，不对应任何真实人物、机构或事件。"
                    )
                else:
                    rows.append(
                        f"Archive record {row_index:03d}: synthetic station "
                        f"S{row_index:03d} has metric {value:03d}; this record is solely for "
                        "internal long-context evaluation and describes no real entity or event."
                    )
            if index < 3:
                target = 173 - index * 11
                answer = str(values[target])
                question = (
                    f"档案记录 {target:03d} 的指标是多少？只输出整数。"
                    if language == Language.ZH
                    else (
                        f"What is the metric in archive record {target:03d}? "
                        "Output only the integer."
                    )
                )
            elif index < 12:
                first = 31 + index
                second = 181 - index
                answer = str(values[first] + values[second])
                question = (
                    f"将档案记录 {first:03d} 与 {second:03d} 的指标相加。只输出整数。"
                    if language == Language.ZH
                    else (
                        f"Add the metrics from archive records {first:03d} and {second:03d}. "
                        "Output only the integer."
                    )
                )
            else:
                targets = [17 + index, 89 + index, 201 - index]
                winning = max(targets, key=lambda target: (values[target], -target))
                answer = f"{winning:03d}"
                rendered_targets = ", ".join(f"{target:03d}" for target in targets)
                question = (
                    f"比较档案记录 {rendered_targets} 的指标，输出指标最大的三位记录编号。"
                    if language == Language.ZH
                    else (
                        f"Compare archive records {rendered_targets} and output the three-digit "
                        "record number with the largest metric."
                    )
                )
            prompt = ("\n".join(rows) + "\n\n问题：" + question) if language == Language.ZH else (
                "\n".join(rows) + "\n\nQuestion: " + question
            )
            records.append(
                _original_record(
                    benchmark="BalancedV2-Original-Long-Context",
                    external_id=f"long-{language.value}-{index + 1:02d}",
                    language=language,
                    ability=Ability.LONG_CONTEXT,
                    question_type=QuestionType.SHORT_ANSWER,
                    prompt=prompt,
                    reference_answer=answer,
                )
            )
    return records


def _original_record(
    *,
    benchmark: str,
    external_id: str,
    language: Language,
    ability: Ability,
    question_type: QuestionType,
    prompt: str,
    reference_answer: str,
    choices: list[Choice] | None = None,
    requires_citations: bool = False,
) -> QuestionRecord:
    return QuestionRecord(
        benchmark=benchmark,
        source=f"internal://balanced-v2/{_slug(benchmark)}",
        source_version=ORIGINAL_SOURCE_VERSION,
        license_note=ORIGINAL_LICENSE,
        sampling_seed=SAMPLING_SEED,
        external_id=external_id,
        language=language,
        ability=ability,
        type=question_type,
        prompt=prompt,
        reference_answer=reference_answer,
        choices=choices,
        requires_citations=requires_citations,
    )


def _stable_record_key(record: QuestionRecord) -> str:
    payload = f"{SAMPLING_SEED}:{record.benchmark}:{record.language.value}:{record.external_id}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
