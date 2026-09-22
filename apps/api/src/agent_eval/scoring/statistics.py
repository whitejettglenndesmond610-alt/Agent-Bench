import random
from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from agent_eval.db.enums import Ability

FULL_SCORE_THRESHOLD = Decimal("99.5")
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 0
CI_LOWER_PERCENTILE = Decimal("2.5")
CI_UPPER_PERCENTILE = Decimal("97.5")


@dataclass(frozen=True)
class QuestionReliability:
    question_mean: Decimal
    trial_successes: int
    trial_count: int


def trial_succeeded(final_score: Decimal) -> bool:
    return final_score >= FULL_SCORE_THRESHOLD


def summarize_question(trial_scores: list[Decimal]) -> QuestionReliability:
    if not trial_scores:
        raise ValueError("cannot summarize a question without trials")
    return QuestionReliability(
        question_mean=sum(trial_scores, start=Decimal(0)) / Decimal(len(trial_scores)),
        trial_successes=sum(trial_succeeded(score) for score in trial_scores),
        trial_count=len(trial_scores),
    )


def pass_at_1(questions: list[QuestionReliability]) -> Decimal:
    if not questions:
        raise ValueError("cannot score an agent without questions")
    return sum(
        (
            Decimal(question.trial_successes) * 100 / Decimal(question.trial_count)
            for question in questions
        ),
        start=Decimal(0),
    ) / Decimal(len(questions))


def pass_at_k(questions: list[QuestionReliability]) -> Decimal:
    if not questions:
        raise ValueError("cannot score an agent without questions")
    return (
        Decimal(sum(question.trial_successes >= 1 for question in questions))
        * 100
        / Decimal(len(questions))
    )


def pass_pow_k(questions: list[QuestionReliability]) -> Decimal:
    if not questions:
        raise ValueError("cannot score an agent without questions")
    return (
        Decimal(
            sum(question.trial_successes == question.trial_count for question in questions)
        )
        * 100
        / Decimal(len(questions))
    )


def stratified_bootstrap_ci(
    questions_by_ability: dict[Ability, list[QuestionReliability]],
    ability_weights: dict[Ability, int],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[Decimal, Decimal]:
    """Percentile CI for the weighted overall score, resampling questions within abilities."""
    strata = {
        ability: questions
        for ability, questions in questions_by_ability.items()
        if questions
    }
    if not strata:
        raise ValueError("cannot bootstrap an agent without questions")
    covered_weight_total = Decimal(sum(ability_weights.get(ability, 0) for ability in strata))
    if covered_weight_total == 0:
        raise ValueError("covered abilities must have a positive configured weight")
    rng = random.Random(seed)
    samples: list[Decimal] = []
    for _ in range(resamples):
        overall = Decimal(0)
        for ability, questions in strata.items():
            drawn = [rng.choice(questions).question_mean for _ in questions]
            stratum_mean = sum(drawn, start=Decimal(0)) / Decimal(len(drawn))
            overall += stratum_mean * Decimal(ability_weights.get(ability, 0))
        samples.append(overall / covered_weight_total)
    samples.sort()
    return (
        _percentile(samples, CI_LOWER_PERCENTILE),
        _percentile(samples, CI_UPPER_PERCENTILE),
    )


def _percentile(sorted_values: list[Decimal], percentile: Decimal) -> Decimal:
    index = ceil(percentile * len(sorted_values) / 100) - 1
    return sorted_values[max(index, 0)]
