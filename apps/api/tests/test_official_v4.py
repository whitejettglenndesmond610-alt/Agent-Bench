from types import SimpleNamespace

from agent_eval.datasets.official_builder import gsm8k_final_answer
from agent_eval.runs.profiles import is_complete_official_v4


def test_gsm8k_final_answer_reads_hash_marker() -> None:
    assert gsm8k_final_answer("reason\n#### 18") == "18"
    assert gsm8k_final_answer("#### 1,234") == "1234"


def _dataset(name: str, count: int) -> SimpleNamespace:
    return SimpleNamespace(benchmark=SimpleNamespace(name=name), question_count=count)


def test_official_v4_completeness_requires_exact_six_benches() -> None:
    datasets = [
        _dataset("GPQA-Diamond", 20),
        _dataset("CMMLU", 20),
        _dataset("GSM8K", 20),
        _dataset("BrowseComp", 10),
        _dataset("BrowseComp-ZH", 20),
        _dataset("FinQA", 10),
    ]
    assert is_complete_official_v4(datasets)
    assert not is_complete_official_v4(datasets[:-1])
    assert not is_complete_official_v4([*datasets[:-1], _dataset("FinQA", 11)])
