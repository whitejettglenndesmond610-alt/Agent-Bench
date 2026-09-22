import csv
import hashlib
import io
import json
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_eval.datasets.demo_builder import fetch_bytes, fetch_json
from agent_eval.datasets.schema import Choice, QuestionRecord
from agent_eval.db.enums import Ability, Language, QuestionType

OFFICIAL_V4_COHORT_KEY = "official-v4"
OFFICIAL_V4_SAMPLING_SEED = 20260814
GPQA_COMMIT = "56686c06f5e19865c153de0fdb11be3890014df7"
GPQA_ZIP_URL = (
    f"https://raw.githubusercontent.com/idavidrein/gpqa/{GPQA_COMMIT}/dataset.zip"
)
GPQA_ZIP_PASSWORD = b"deserted-untie-orchid"
CMMLU_COMMIT = "d6e7b716d8ac694f38969a6c0407437d1fded799"
CMMLU_TEST_API = (
    "https://api.github.com/repos/haonan-li/CMMLU/contents/data/test?ref=" + CMMLU_COMMIT
)
GSM8K_COMMIT = "3101c7d5072418e28b9008a6636bde82a006892c"
GSM8K_URL = (
    "https://raw.githubusercontent.com/openai/grade-school-math/"
    f"{GSM8K_COMMIT}/grade_school_math/data/test.jsonl"
)
OFFICIAL_V4_QUOTAS = {
    "GPQA-Diamond": 20,
    "CMMLU": 20,
    "GSM8K": 20,
}


class OfficialDatasetBuildError(RuntimeError):
    pass


def stable_hash(value: str) -> bytes:
    return hashlib.sha256(f"{OFFICIAL_V4_SAMPLING_SEED}|{value}".encode()).digest()


def _choice_records(options: list[str]) -> list[Choice]:
    return [
        Choice(label=chr(ord("A") + index), text=option.strip())
        for index, option in enumerate(options)
    ]


def _write_records(output_directory: Path, records: list[QuestionRecord]) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[QuestionRecord]] = defaultdict(list)
    for record in records:
        groups[record.benchmark].append(record)
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "cohort_key": OFFICIAL_V4_COHORT_KEY,
        "sampling_seed": OFFICIAL_V4_SAMPLING_SEED,
        "question_count": len(records),
        "benchmarks": {},
    }
    for benchmark, group in sorted(groups.items()):
        filename = benchmark.lower().replace("-", "_") + ".jsonl"
        content = "".join(
            record.model_dump_json() + "\n"
            for record in sorted(group, key=lambda item: item.external_id)
        )
        (output_directory / filename).write_text(content, encoding="utf-8")
        manifest["benchmarks"][benchmark] = {
            "file": filename,
            "question_count": len(group),
            "file_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "source_version": group[0].source_version,
        }
    (output_directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_gpqa_records(zip_bytes: bytes | None = None) -> list[QuestionRecord]:
    payload = zip_bytes if zip_bytes is not None else fetch_bytes(GPQA_ZIP_URL)
    archive = zipfile.ZipFile(io.BytesIO(payload))
    archive.setpassword(GPQA_ZIP_PASSWORD)
    rows = list(
        csv.DictReader(io.TextIOWrapper(archive.open("dataset/gpqa_diamond.csv"), encoding="utf-8"))
    )
    if len(rows) < OFFICIAL_V4_QUOTAS["GPQA-Diamond"]:
        raise OfficialDatasetBuildError("GPQA Diamond is smaller than the official-v4 quota")
    indices = sorted(
        range(len(rows)),
        key=lambda index: stable_hash(f"gpqa|{rows[index].get('Record ID', index)}"),
    )[: OFFICIAL_V4_QUOTAS["GPQA-Diamond"]]
    records: list[QuestionRecord] = []
    for index in sorted(indices):
        row = rows[index]
        options = [
            row["Correct Answer"],
            row["Incorrect Answer 1"],
            row["Incorrect Answer 2"],
            row["Incorrect Answer 3"],
        ]
        order = sorted(
            range(4),
            key=lambda item: stable_hash(f"gpqa-choice|{row['Record ID']}|{item}"),
        )
        shuffled = [options[item].strip() for item in order]
        answer = chr(ord("A") + shuffled.index(row["Correct Answer"].strip()))
        records.append(
            QuestionRecord(
                benchmark="GPQA-Diamond",
                source="https://github.com/idavidrein/gpqa",
                source_version=GPQA_COMMIT,
                license_note="CC BY 4.0; attribution required; keep local copies for research use",
                sampling_seed=OFFICIAL_V4_SAMPLING_SEED,
                external_id=str(row["Record ID"]),
                language=Language.EN,
                ability=Ability.REASONING,
                type=QuestionType.MULTIPLE_CHOICE,
                prompt=row["Question"].strip(),
                reference_answer=answer,
                choices=_choice_records(shuffled),
                difficulty=(row.get("Writer's Difficulty Estimate") or "")[:80] or None,
                domain=row.get("High-level domain") or "science",
            )
        )
    return records


def _cmmlu_record(subject: str, text: str) -> QuestionRecord:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise OfficialDatasetBuildError(f"CMMLU subject {subject} is empty")
    row_index = int.from_bytes(stable_hash(f"cmmlu|{subject}")) % len(rows)
    row = rows[row_index]
    return QuestionRecord(
        benchmark="CMMLU",
        source="https://github.com/haonan-li/CMMLU",
        source_version=CMMLU_COMMIT,
        license_note="CC BY-NC 4.0; noncommercial research use and attribution required",
        sampling_seed=OFFICIAL_V4_SAMPLING_SEED,
        external_id=f"{subject}:{row_index}",
        language=Language.ZH,
        ability=Ability.REASONING,
        type=QuestionType.MULTIPLE_CHOICE,
        prompt=str(row["Question"]).strip(),
        reference_answer=str(row["Answer"]).strip(),
        choices=_choice_records([str(row[label]) for label in "ABCD"]),
        domain=subject,
    )


def build_cmmlu_records(subject_csvs: dict[str, str] | None = None) -> list[QuestionRecord]:
    if subject_csvs is None:
        listing = fetch_json(CMMLU_TEST_API)
        subjects = sorted(
            Path(item["name"]).stem
            for item in listing
            if item.get("name", "").endswith(".csv")
        )
        if len(subjects) < OFFICIAL_V4_QUOTAS["CMMLU"]:
            raise OfficialDatasetBuildError(
                "CMMLU subject list is smaller than the official-v4 quota"
            )
        selected = sorted(subjects, key=stable_hash)[: OFFICIAL_V4_QUOTAS["CMMLU"]]
        subject_csvs = {}
        for subject in selected:
            url = (
                "https://raw.githubusercontent.com/haonan-li/CMMLU/"
                f"{CMMLU_COMMIT}/data/test/{quote(subject)}.csv"
            )
            subject_csvs[subject] = fetch_bytes(url).decode("utf-8")
    if len(subject_csvs) != OFFICIAL_V4_QUOTAS["CMMLU"]:
        raise OfficialDatasetBuildError("CMMLU input must contain exactly 20 subjects")
    return [_cmmlu_record(subject, text) for subject, text in sorted(subject_csvs.items())]


def gsm8k_final_answer(answer: str) -> str:
    marker = "####"
    if marker not in answer:
        raise OfficialDatasetBuildError("GSM8K answer is missing #### marker")
    return answer.rsplit(marker, 1)[1].strip().replace(",", "")


def build_gsm8k_records(raw_text: str | None = None) -> list[QuestionRecord]:
    text = raw_text if raw_text is not None else fetch_bytes(GSM8K_URL).decode("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if len(rows) < OFFICIAL_V4_QUOTAS["GSM8K"]:
        raise OfficialDatasetBuildError("GSM8K test split is smaller than the official-v4 quota")
    indices = sorted(range(len(rows)), key=lambda index: stable_hash(f"gsm8k|{index}"))[
        : OFFICIAL_V4_QUOTAS["GSM8K"]
    ]
    return [
        QuestionRecord(
            benchmark="GSM8K",
            source="https://github.com/openai/grade-school-math",
            source_version=GSM8K_COMMIT,
            license_note="MIT; preserve attribution",
            sampling_seed=OFFICIAL_V4_SAMPLING_SEED,
            external_id=str(index),
            language=Language.EN,
            ability=Ability.DATA_REASONING,
            type=QuestionType.SHORT_ANSWER,
            prompt=str(rows[index]["question"]).strip(),
            reference_answer=gsm8k_final_answer(str(rows[index]["answer"])),
            domain="grade-school-math",
        )
        for index in sorted(indices)
    ]


def build_official_v4_dataset(output_directory: Path) -> list[QuestionRecord]:
    records = [
        *build_gpqa_records(),
        *build_cmmlu_records(),
        *build_gsm8k_records(),
    ]
    counts: defaultdict[str, int] = defaultdict(int)
    for record in records:
        counts[record.benchmark] += 1
    if dict(counts) != OFFICIAL_V4_QUOTAS:
        raise OfficialDatasetBuildError(f"unexpected official-v4 quotas: {dict(counts)}")
    _write_records(output_directory, records)
    return records
