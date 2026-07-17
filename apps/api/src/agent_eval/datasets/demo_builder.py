import base64
import csv
import hashlib
import io
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_eval.datasets.schema import Choice, QuestionRecord, validate_demo_distribution
from agent_eval.db.enums import Ability, Language, QuestionType

SAMPLING_SEED = 20260716
USER_AGENT = "agent-evaluation-platform/0.1 dataset-builder"
HF_ROWS_URL = "https://datasets-server.huggingface.co"

CEVAL_REPO = "ceval/ceval-exam"
CEVAL_REVISION = "617524a00b307ff6f9933702f724131fe12ca7ce"
CEVAL_MAPPING_URL = (
    "https://raw.githubusercontent.com/hkust-nlp/ceval/"
    "cba65ae93bcf189149ced9f66ae0c958201faed9/subject_mapping.json"
)
MMLU_REPO = "TIGER-Lab/MMLU-Pro"
MMLU_REVISION = "b189ec765aa7ed75c8acfea42df31fdae71f97be"
BROWSE_ZH_REPO = "PALIN2018/BrowseComp-ZH"
BROWSE_ZH_REVISION = "15b7bffc8af684c9b012843fb4f6353838ea3357"
BROWSE_URL = "https://openaipublic.blob.core.windows.net/simple-evals/browse_comp_test_set.csv"
BROWSE_SHA256 = "7b24471cd5b3eb2a46830a14802b5c029ea62f488ff75a0f88af7923d1454abf"
BIZFIN_REVISION = "16983117cd5022097d672f06cb951b9e981754e4"
FINQA_REVISION = "0f16e2867befa6840783e58be38c9efb9229d742"
FINQA_URL = f"https://raw.githubusercontent.com/czyssrs/FinQA/{FINQA_REVISION}/dataset/test.json"

BIZFIN_FILES = {
    "financial_quantitative_computation_cn": QuestionType.SHORT_ANSWER,
    "event_logic_reasoning_cn": QuestionType.SHORT_ANSWER,
    "anomaly_information_tracing_cn": QuestionType.SHORT_ANSWER,
    "financial_data_description_cn": QuestionType.OPEN_ENDED,
    "financial_report_analysis": QuestionType.OPEN_ENDED,
}
MMLU_CATEGORIES = [
    "biology",
    "business",
    "chemistry",
    "computer science",
    "economics",
    "engineering",
    "health",
    "history",
    "law",
    "math",
    "other",
    "philosophy",
    "physics",
    "psychology",
]


class DemoDatasetBuildError(RuntimeError):
    pass


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(7):
        time.sleep(0.2)
        try:
            with urlopen(request, timeout=120) as response:
                return cast(bytes, response.read())
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            if attempt == 6:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2**attempt, 30)
            time.sleep(delay)
    raise DemoDatasetBuildError(f"failed to download {url}")


def fetch_json(url: str) -> Any:
    return json.loads(fetch_bytes(url))


def stable_hash(value: str) -> bytes:
    return hashlib.sha256(f"{SAMPLING_SEED}|{value}".encode()).digest()


def verify_huggingface_revision(repo: str, expected_revision: str) -> dict[str, Any]:
    metadata = fetch_json(f"https://huggingface.co/api/datasets/{repo}")
    if metadata.get("sha") != expected_revision:
        raise DemoDatasetBuildError(
            f"{repo} moved from pinned revision {expected_revision}; review before updating"
        )
    return cast(dict[str, Any], metadata)


def hf_rows(
    repo: str,
    *,
    config: str,
    offset: int,
    length: int,
    where: str | None = None,
) -> dict[str, Any]:
    endpoint = "filter" if where is not None else "rows"
    parameters: dict[str, str | int] = {
        "dataset": repo,
        "config": config,
        "split": "test",
        "offset": offset,
        "length": length,
    }
    if where is not None:
        parameters["where"] = where
    return cast(dict[str, Any], fetch_json(f"{HF_ROWS_URL}/{endpoint}?{urlencode(parameters)}"))


def choice_records(options: list[Any]) -> list[Choice]:
    return [
        Choice(label=chr(ord("A") + index), text=str(option))
        for index, option in enumerate(options)
    ]


def build_ceval_records() -> list[QuestionRecord]:
    metadata = verify_huggingface_revision(CEVAL_REPO, CEVAL_REVISION)
    subject_mapping: dict[str, list[str]] = fetch_json(CEVAL_MAPPING_URL)
    quotas = {"STEM": 12, "Social Science": 6, "Humanities": 6, "Other": 6}
    selected_subjects: list[str] = []
    for category, quota in quotas.items():
        subjects = [subject for subject, values in subject_mapping.items() if values[2] == category]
        selected_subjects.extend(sorted(subjects, key=stable_hash)[:quota])

    dataset_info = metadata["cardData"]["dataset_info"]
    row_counts = {
        config["config_name"]: next(
            split["num_examples"] for split in config["splits"] if split["name"] == "test"
        )
        for config in dataset_info
    }
    records: list[QuestionRecord] = []
    for subject in sorted(selected_subjects):
        row_index = int.from_bytes(stable_hash(f"ceval|{subject}")) % row_counts[subject]
        row = hf_rows(CEVAL_REPO, config=subject, offset=row_index, length=1)["rows"][0]["row"]
        records.append(
            QuestionRecord(
                benchmark="C-Eval",
                source=f"https://huggingface.co/datasets/{CEVAL_REPO}",
                source_version=CEVAL_REVISION,
                license_note="CC BY-NC-SA 4.0; noncommercial use and attribution required",
                sampling_seed=SAMPLING_SEED,
                external_id=f"{subject}:{row['id']}",
                language=Language.ZH,
                ability=Ability.REASONING,
                type=QuestionType.MULTIPLE_CHOICE,
                prompt=row["question"],
                reference_answer=row["answer"],
                choices=choice_records([row[label] for label in "ABCD"]),
                domain=subject,
            )
        )
    return records


def build_mmlu_records() -> list[QuestionRecord]:
    verify_huggingface_revision(MMLU_REPO, MMLU_REVISION)
    categories = sorted(MMLU_CATEGORIES, key=stable_hash)[:10]
    records: list[QuestionRecord] = []
    for category in sorted(categories):
        where = f"\"category\"='{category}'"
        first_page = hf_rows(MMLU_REPO, config="default", offset=0, length=1, where=where)
        row_index = int.from_bytes(stable_hash(f"mmlu|{category}")) % first_page["num_rows_total"]
        page = (
            first_page
            if row_index == 0
            else hf_rows(MMLU_REPO, config="default", offset=row_index, length=1, where=where)
        )
        row = page["rows"][0]["row"]
        records.append(
            QuestionRecord(
                benchmark="MMLU-Pro",
                source=f"https://huggingface.co/datasets/{MMLU_REPO}",
                source_version=MMLU_REVISION,
                license_note="MIT dataset release; preserve attribution and review source rights",
                sampling_seed=SAMPLING_SEED,
                external_id=str(row["question_id"]),
                language=Language.EN,
                ability=Ability.REASONING,
                type=QuestionType.MULTIPLE_CHOICE,
                prompt=row["question"],
                reference_answer=row["answer"],
                choices=choice_records(row["options"]),
                domain=category,
            )
        )
    return records


def decrypt_browsecomp(value: str, canary: str) -> str:
    encrypted = base64.b64decode(value)
    key = hashlib.sha256(canary.encode()).digest()
    decrypted = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(encrypted))
    return decrypted.decode()


def build_browse_records() -> list[QuestionRecord]:
    content = fetch_bytes(BROWSE_URL)
    if hashlib.sha256(content).hexdigest() != BROWSE_SHA256:
        raise DemoDatasetBuildError("BrowseComp source checksum changed")
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    indices = sorted(range(len(rows)), key=lambda index: stable_hash(f"browse|{index}"))[:10]
    return [
        QuestionRecord(
            benchmark="BrowseComp",
            source=BROWSE_URL,
            source_version=f"sha256:{BROWSE_SHA256}",
            license_note=(
                "MIT evaluator; keep decrypted examples local to prevent benchmark leakage"
            ),
            sampling_seed=SAMPLING_SEED,
            external_id=str(index),
            language=Language.EN,
            ability=Ability.RETRIEVAL,
            type=QuestionType.SHORT_ANSWER,
            prompt=decrypt_browsecomp(rows[index]["problem"], rows[index]["canary"]),
            reference_answer=decrypt_browsecomp(rows[index]["answer"], rows[index]["canary"]),
            requires_citations=True,
            domain=rows[index]["problem_topic"],
        )
        for index in sorted(indices)
    ]


def build_browse_zh_records() -> list[QuestionRecord]:
    verify_huggingface_revision(BROWSE_ZH_REPO, BROWSE_ZH_REVISION)
    rows: list[dict[str, Any]] = []
    for offset in range(0, 289, 100):
        page = hf_rows(BROWSE_ZH_REPO, config="default", offset=offset, length=100)
        rows.extend(item["row"] for item in page["rows"])
    if len(rows) != 289:
        raise DemoDatasetBuildError(f"expected 289 BrowseComp-ZH rows, got {len(rows)}")
    indices = sorted(range(len(rows)), key=lambda index: stable_hash(f"browse-zh|{index}"))[:20]
    return [
        QuestionRecord(
            benchmark="BrowseComp-ZH",
            source=f"https://huggingface.co/datasets/{BROWSE_ZH_REPO}",
            source_version=BROWSE_ZH_REVISION,
            license_note=(
                "License metadata conflicts; research/non-high-stakes use only, "
                "attribution required; "
                "keep decrypted examples local"
            ),
            sampling_seed=SAMPLING_SEED,
            external_id=str(index),
            language=Language.ZH,
            ability=Ability.RETRIEVAL,
            type=QuestionType.SHORT_ANSWER,
            prompt=decrypt_browsecomp(rows[index]["Question"], rows[index]["canary"]),
            reference_answer=decrypt_browsecomp(rows[index]["Answer"], rows[index]["canary"]),
            requires_citations=True,
            domain=decrypt_browsecomp(rows[index]["Topic"], rows[index]["canary"]),
        )
        for index in sorted(indices)
    ]


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    return "\n".join(str(item["text"]) for item in content if "text" in item)


def build_bizfin_records() -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    for subset, question_type in BIZFIN_FILES.items():
        url = (
            "https://huggingface.co/datasets/HiThink-Research/BizFinBench.v2/resolve/"
            f"{BIZFIN_REVISION}/cn/{subset}.jsonl?download=true"
        )
        lines = [line for line in fetch_bytes(url).splitlines() if line.strip()]
        selected = sorted(enumerate(lines), key=lambda item: hashlib.sha256(item[1]).digest())[:4]
        for line_number, raw_line in selected:
            source = json.loads(raw_line)
            prompt = "\n\n".join(
                message_text(message) for message in source["messages"] if message_text(message)
            )
            answer = message_text(source["choices"][0]["message"])
            records.append(
                QuestionRecord(
                    benchmark="BizFinBench",
                    source="https://huggingface.co/datasets/HiThink-Research/BizFinBench.v2",
                    source_version=BIZFIN_REVISION,
                    license_note=(
                        "CC BY-NC 4.0; research/noncommercial use and attribution required"
                    ),
                    sampling_seed=SAMPLING_SEED,
                    external_id=f"{subset}:{line_number + 1}",
                    language=Language.ZH,
                    ability=Ability.DATA_REASONING,
                    type=question_type,
                    prompt=prompt,
                    reference_answer=answer,
                    domain=subset,
                )
            )
    return records


def finqa_evidence_type(record: dict[str, Any]) -> str:
    keys = record["qa"]["gold_inds"]
    has_table = any(key.startswith("table_") for key in keys)
    has_text = any(key.startswith("text_") for key in keys)
    if has_table and has_text:
        return "hybrid"
    return "table" if has_table else "text"


def build_finqa_records() -> list[QuestionRecord]:
    source_records: list[dict[str, Any]] = fetch_json(FINQA_URL)
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in source_records:
        if not str(record["qa"].get("answer", "")).strip():
            continue
        complexity = "one-step" if len(record["qa"]["steps"]) == 1 else "multi-step"
        strata[(finqa_evidence_type(record), complexity)].append(record)

    quotas = {
        ("table", "one-step"): 2,
        ("table", "multi-step"): 2,
        ("text", "one-step"): 2,
        ("text", "multi-step"): 1,
        ("hybrid", "one-step"): 1,
        ("hybrid", "multi-step"): 2,
    }
    selected: list[dict[str, Any]] = []
    for stratum, quota in quotas.items():
        selected.extend(sorted(strata[stratum], key=lambda item: stable_hash(item["id"]))[:quota])

    records: list[QuestionRecord] = []
    for source in sorted(selected, key=lambda item: item["id"]):
        qa = source["qa"]
        evidence = "\n".join(str(value) for value in qa["gold_inds"].values())
        records.append(
            QuestionRecord(
                benchmark="FinQA",
                source="https://github.com/czyssrs/FinQA",
                source_version=FINQA_REVISION,
                license_note="CC BY 4.0 data; attribution and modification notice required",
                sampling_seed=SAMPLING_SEED,
                external_id=source["id"],
                language=Language.EN,
                ability=Ability.DATA_REASONING,
                type=QuestionType.SHORT_ANSWER,
                prompt=f"Evidence:\n{evidence}\n\nQuestion:\n{qa['question']}",
                reference_answer=str(qa["answer"]),
                difficulty="one-step" if len(qa["steps"]) == 1 else "multi-step",
                domain=finqa_evidence_type(source),
            )
        )
    return records


def build_demo_dataset(output_directory: Path) -> list[QuestionRecord]:
    records = [
        *build_ceval_records(),
        *build_mmlu_records(),
        *build_browse_zh_records(),
        *build_browse_records(),
        *build_bizfin_records(),
        *build_finqa_records(),
    ]
    validate_demo_distribution(records)

    output_directory.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[QuestionRecord]] = defaultdict(list)
    for record in records:
        groups[record.benchmark].append(record)

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "sampling_seed": SAMPLING_SEED,
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
    return records
