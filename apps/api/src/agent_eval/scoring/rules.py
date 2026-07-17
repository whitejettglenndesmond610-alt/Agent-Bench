import re
import unicodedata
from dataclasses import dataclass

EXPLICIT_ANSWER_PATTERN = re.compile(
    r"(?:最终答案|答案|选项|final\s+answer|answer|option)"
    r"\s*(?:is|为|是|[:：])?\s*[\(\[【]?([A-Z])[\)\]】]?",
    re.IGNORECASE,
)
STANDALONE_LABEL_PATTERN = re.compile(r"(?<![A-Z])([A-Z])(?![A-Z])")
AMBIGUITY_PATTERN = re.compile(r"(?:\bor\b|或者|或是|也可能|/)", re.IGNORECASE)


@dataclass(frozen=True)
class RuleScore:
    score: int
    reason: str
    extracted_answer: str | None


def extract_choice_label(answer: str, valid_labels: list[str]) -> str | None:
    normalized = unicodedata.normalize("NFKC", answer).strip().upper()
    labels = {label.upper() for label in valid_labels}
    exact = normalized.strip(" \t\r\n.。()[]【】:：")
    if exact in labels:
        return exact

    explicit = [str(match).upper() for match in EXPLICIT_ANSWER_PATTERN.findall(normalized)]
    explicit_valid = set(explicit) & labels
    if len(explicit_valid) == 1:
        all_labels = {str(match) for match in STANDALONE_LABEL_PATTERN.findall(normalized)} & labels
        if AMBIGUITY_PATTERN.search(normalized) and len(all_labels) > 1:
            return None
        return next(iter(explicit_valid))
    if len(explicit_valid) > 1:
        return None

    standalone = {str(match) for match in STANDALONE_LABEL_PATTERN.findall(normalized)} & labels
    return next(iter(standalone)) if len(standalone) == 1 else None


def score_multiple_choice(answer: str, reference_answer: str, valid_labels: list[str]) -> RuleScore:
    extracted = extract_choice_label(answer, valid_labels)
    reference = reference_answer.strip().upper()
    if extracted is None:
        return RuleScore(score=0, reason="无法提取唯一选项", extracted_answer=None)
    if extracted == reference:
        return RuleScore(score=100, reason="提取选项与标准答案一致", extracted_answer=extracted)
    return RuleScore(
        score=0,
        reason=f"提取选项 {extracted} 与标准答案不一致",
        extracted_answer=extracted,
    )


def score_exact_answer(answer: str, reference_answer: str) -> RuleScore:
    candidate = answer.strip()
    reference = reference_answer.strip()
    if candidate == reference:
        return RuleScore(score=100, reason="答案严格匹配格式要求", extracted_answer=candidate)
    return RuleScore(score=0, reason="答案与严格格式标准答案不一致", extracted_answer=candidate)
