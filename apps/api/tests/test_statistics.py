from decimal import Decimal

import pytest

from agent_eval.db.enums import Ability
from agent_eval.scoring.statistics import (
    BOOTSTRAP_RESAMPLES,
    FULL_SCORE_THRESHOLD,
    QuestionReliability,
    pass_at_1,
    pass_at_k,
    pass_pow_k,
    stratified_bootstrap_ci,
    summarize_question,
    trial_succeeded,
)

WEIGHTS = {
    Ability.REASONING: 40,
    Ability.RETRIEVAL: 35,
    Ability.DATA_REASONING: 25,
    Ability.INSTRUCTION_FOLLOWING: 0,
    Ability.LONG_CONTEXT: 0,
}


def question(*scores: int) -> QuestionReliability:
    return summarize_question([Decimal(score) for score in scores])


def test_trial_success_uses_full_score_threshold() -> None:
    assert trial_succeeded(Decimal("100")) is True
    assert trial_succeeded(FULL_SCORE_THRESHOLD) is True
    assert trial_succeeded(Decimal("99.4")) is False
    assert trial_succeeded(Decimal("0")) is False


def test_summarize_question_averages_trials_and_counts_successes() -> None:
    summary = summarize_question([Decimal("100"), Decimal("50"), Decimal("0")])

    assert summary.question_mean == Decimal("50")
    assert summary.trial_successes == 1
    assert summary.trial_count == 3


def test_summarize_question_rejects_empty_trials() -> None:
    with pytest.raises(ValueError, match="without trials"):
        summarize_question([])


def test_pass_metrics_match_hand_computed_values() -> None:
    questions = [
        question(100, 100, 100),  # all 3 trials succeed
        question(100, 0, 100),  # 2 of 3 trials succeed
        question(0, 0, 100),  # 1 of 3 trials succeed
        question(0, 50, 0),  # no trial succeeds
    ]

    # per-question success rates: 1, 2/3, 1/3, 0 -> mean 0.5
    assert pass_at_1(questions) == Decimal("50")
    # questions with at least one success: 3 of 4
    assert pass_at_k(questions) == Decimal("75")
    # questions where every trial succeeds: 1 of 4
    assert pass_pow_k(questions) == Decimal("25")


def test_pass_metrics_degenerate_to_single_run_semantics_when_k_is_one() -> None:
    questions = [question(100), question(0), question(100), question(75)]

    assert pass_at_1(questions) == Decimal("50")
    assert pass_at_k(questions) == pass_at_1(questions)
    assert pass_pow_k(questions) == pass_at_1(questions)


def test_pass_metrics_reject_empty_agents() -> None:
    with pytest.raises(ValueError, match="without questions"):
        pass_at_1([])
    with pytest.raises(ValueError, match="without questions"):
        pass_at_k([])
    with pytest.raises(ValueError, match="without questions"):
        pass_pow_k([])


def test_bootstrap_ci_is_reproducible_with_fixed_seed() -> None:
    strata = {
        Ability.REASONING: [question(100, 0), question(50, 50), question(0, 100)],
        Ability.RETRIEVAL: [question(100, 100), question(0, 0)],
    }

    first = stratified_bootstrap_ci(strata, WEIGHTS)
    second = stratified_bootstrap_ci(strata, WEIGHTS)

    assert first == second
    low, high = first
    assert Decimal("0") <= low <= high <= Decimal("100")


def test_bootstrap_ci_collapses_when_all_questions_score_identically() -> None:
    strata = {
        Ability.REASONING: [question(80), question(80)],
        Ability.RETRIEVAL: [question(20)],
    }

    low, high = stratified_bootstrap_ci(strata, WEIGHTS)

    # every resample reproduces the weighted overall: (80*40 + 20*35) / 75
    assert low == high == Decimal("52")


def test_bootstrap_ci_honors_resample_count_and_custom_seed() -> None:
    strata = {Ability.REASONING: [question(100), question(0), question(50)]}

    ci = stratified_bootstrap_ci(strata, WEIGHTS, resamples=50, seed=7)
    repeat = stratified_bootstrap_ci(strata, WEIGHTS, resamples=50, seed=7)

    assert ci == repeat
    assert BOOTSTRAP_RESAMPLES == 1000


def test_bootstrap_ci_rejects_empty_or_zero_weight_strata() -> None:
    with pytest.raises(ValueError, match="without questions"):
        stratified_bootstrap_ci({}, WEIGHTS)
    with pytest.raises(ValueError, match="positive configured weight"):
        stratified_bootstrap_ci(
            {Ability.INSTRUCTION_FOLLOWING: [question(100)]},
            WEIGHTS,
        )
