from dataclasses import dataclass

JUDGE_PASS_THRESHOLD = 60.0
MIN_RELIABLE_SAMPLE = 30


def judge_passed(score: float) -> bool:
    return score >= JUDGE_PASS_THRESHOLD


def cohens_kappa(judge_calls: list[bool], agreements: list[bool]) -> float | None:
    """Cohen's kappa between the judge's pass/fail call and the human labeler.

    The human call is derived from the agree/disagree label: agreeing means the human
    makes the same call as the judge, disagreeing means the opposite call.
    Returns None when kappa is undefined (no pairs or degenerate marginals).
    """
    if len(judge_calls) != len(agreements):
        raise ValueError("judge calls and agreements must have the same length")
    total = len(judge_calls)
    if total == 0:
        return None
    both_pass = 0
    judge_pass_only = 0
    human_pass_only = 0
    both_fail = 0
    for judge_call, agree in zip(judge_calls, agreements, strict=True):
        human_call = judge_call if agree else not judge_call
        if judge_call and human_call:
            both_pass += 1
        elif judge_call:
            judge_pass_only += 1
        elif human_call:
            human_pass_only += 1
        else:
            both_fail += 1
    observed = (both_pass + both_fail) / total
    judge_pass_rate = (both_pass + judge_pass_only) / total
    human_pass_rate = (both_pass + human_pass_only) / total
    expected = judge_pass_rate * human_pass_rate + (1 - judge_pass_rate) * (1 - human_pass_rate)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class CalibrationSummary:
    labeled: int
    agree: int
    disagree: int
    agreement: float | None
    kappa: float | None
    insufficient_sample: bool


def summarize_calibration(pairs: list[tuple[float, bool]]) -> CalibrationSummary:
    """Summarize labeled calibration pairs of (judge score, human agree flag)."""
    labeled = len(pairs)
    agree = sum(1 for _, human_agree in pairs if human_agree)
    kappa = cohens_kappa(
        [judge_passed(score) for score, _ in pairs],
        [human_agree for _, human_agree in pairs],
    )
    return CalibrationSummary(
        labeled=labeled,
        agree=agree,
        disagree=labeled - agree,
        agreement=agree / labeled if labeled else None,
        kappa=kappa,
        insufficient_sample=labeled < MIN_RELIABLE_SAMPLE,
    )
