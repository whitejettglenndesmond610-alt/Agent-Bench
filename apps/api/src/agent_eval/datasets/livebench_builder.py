"""LiveBench held-out dataset builder (P1-2 anti-contamination stage 1).

Data source: HuggingFace ``livebench/{math,reasoning,data_analysis}`` datasets
(config ``default``, split ``test``), fetched through the datasets-server rows
API exactly like the demo builder, with each category repo pinned to an
immutable revision. LiveBench rotates questions monthly; the per-category
``release`` parameter selects the ``livebench_release_date`` cohort, so a later
rotation only requires re-running ``build-livebench`` with a newer release and
re-importing (the importer then creates ``version + 1`` for the same benchmark
and profiles pick up the latest frozen version automatically).

Category mapping — only the three released abilities are used:

- ``math`` -> ``LiveBench-Math`` (ability ``reasoning``): competition math with
  exact ground-truth answers; graded as short-answer reasoning like GSM8K.
- ``reasoning`` -> ``LiveBench-Reasoning`` (ability ``reasoning``).
- ``data_analysis`` -> ``LiveBench-DataAnalysis`` (ability ``data_reasoning``):
  table reformat/join/column-type tasks whose prompts embed the full table.

``coding``, ``instruction_following`` and ``language`` are skipped: the
platform has no released ability for them and the importer gate would reject
the records.
"""

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from agent_eval.datasets.demo_builder import hf_rows, verify_huggingface_revision
from agent_eval.datasets.schema import QuestionRecord
from agent_eval.db.enums import Ability, Language, QuestionType

HELDOUT_V1_COHORT_KEY = "heldout-v1"
LIVEBENCH_SAMPLING_SEED = 20260922
LIVEBENCH_SOURCE = "https://huggingface.co/datasets/livebench"
LIVEBENCH_LICENSE = "Apache 2.0; attribution required; keep local copies for research use"

LIVEBENCH_CATEGORIES: dict[str, dict[str, Any]] = {
    "math": {
        "repo": "livebench/math",
        "revision": "bb66571c8ccf32d3df9e6f48b920d3770ff4aacb",
        "release": "2024-08-31",
        "benchmark": "LiveBench-Math",
        "ability": Ability.REASONING,
    },
    "reasoning": {
        "repo": "livebench/reasoning",
        "revision": "6fc6498a5dfba553f69f4413feabade1f1a2d384",
        "release": "2024-11-25",
        "benchmark": "LiveBench-Reasoning",
        "ability": Ability.REASONING,
    },
    "data_analysis": {
        "repo": "livebench/data_analysis",
        "revision": "31b9661ff678df9958e2f7fa228427f4c858c1a1",
        "release": "2024-06-24",
        "benchmark": "LiveBench-DataAnalysis",
        "ability": Ability.DATA_REASONING,
    },
}
LIVEBENCH_QUOTAS = {
    "LiveBench-Math": 20,
    "LiveBench-Reasoning": 20,
    "LiveBench-DataAnalysis": 20,
}


class LiveBenchBuildError(RuntimeError):
    pass


def stable_hash(value: str) -> bytes:
    return hashlib.sha256(f"{LIVEBENCH_SAMPLING_SEED}|{value}".encode()).digest()


def livebench_release_date(row: dict[str, Any]) -> str:
    return str(row.get("livebench_release_date") or "")[:10]


def select_livebench_rows(
    rows: list[dict[str, Any]],
    *,
    release: str,
    quota: int,
) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in rows
        if livebench_release_date(row) == release
        and not str(row.get("livebench_removal_date") or "").strip()
        and str(row.get("ground_truth") or "").strip()
        and len(row.get("turns") or []) == 1
        and str(row["turns"][0]).strip()
    ]
    if len(eligible) < quota:
        raise LiveBenchBuildError(
            f"LiveBench release {release} has {len(eligible)} eligible questions, "
            f"below the quota of {quota}"
        )
    return sorted(eligible, key=lambda row: stable_hash(f"livebench|{row['question_id']}"))[
        :quota
    ]


def livebench_record(category: str, row: dict[str, Any], *, release: str) -> QuestionRecord:
    spec = LIVEBENCH_CATEGORIES[category]
    return QuestionRecord(
        benchmark=spec["benchmark"],
        source=f"{LIVEBENCH_SOURCE}/{category}",
        source_version=f"{spec['revision']}|release={release}",
        license_note=LIVEBENCH_LICENSE,
        sampling_seed=LIVEBENCH_SAMPLING_SEED,
        external_id=str(row["question_id"]),
        language=Language.EN,
        ability=spec["ability"],
        type=QuestionType.SHORT_ANSWER,
        prompt=str(row["turns"][0]).strip(),
        reference_answer=str(row["ground_truth"]).strip(),
        domain=str(row.get("task") or category),
    )


def build_livebench_category_records(
    category: str,
    rows: list[dict[str, Any]] | None = None,
    *,
    release: str | None = None,
) -> list[QuestionRecord]:
    spec = LIVEBENCH_CATEGORIES[category]
    release = release or spec["release"]
    if rows is None:
        verify_huggingface_revision(spec["repo"], spec["revision"])
        rows = []
        offset = 0
        while True:
            page = hf_rows(spec["repo"], config="default", offset=offset, length=100)
            rows.extend(item["row"] for item in page["rows"])
            offset += len(page["rows"])
            if offset >= page["num_rows_total"] or not page["rows"]:
                break
    quota = LIVEBENCH_QUOTAS[spec["benchmark"]]
    selected = select_livebench_rows(rows, release=release, quota=quota)
    return [
        livebench_record(category, row, release=release)
        for row in sorted(selected, key=lambda item: str(item["question_id"]))
    ]


def _write_records(output_directory: Path, records: list[QuestionRecord]) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[QuestionRecord]] = defaultdict(list)
    for record in records:
        groups[record.benchmark].append(record)
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "cohort_key": HELDOUT_V1_COHORT_KEY,
        "sampling_seed": LIVEBENCH_SAMPLING_SEED,
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


def build_livebench_dataset(
    output_directory: Path,
    *,
    releases: dict[str, str] | None = None,
) -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for category in sorted(LIVEBENCH_CATEGORIES):
        records.extend(
            build_livebench_category_records(
                category,
                release=(releases or {}).get(category),
            )
        )
    counts: defaultdict[str, int] = defaultdict(int)
    for record in records:
        counts[record.benchmark] += 1
    if dict(counts) != LIVEBENCH_QUOTAS:
        raise LiveBenchBuildError(f"unexpected LiveBench quotas: {dict(counts)}")
    _write_records(output_directory, records)
    return records
