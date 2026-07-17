from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

from agent_eval.db.enums import Ability, Language
from agent_eval.scoring.aggregation import (
    AggregateAttempt,
    AggregateItem,
    aggregate_agent,
    build_rankings,
)


def item(
    benchmark_id: UUID,
    benchmark_name: str,
    ability: Ability,
    score: int,
    *,
    language: Language = Language.ZH,
    succeeded: bool = True,
    calls: int = 1,
) -> AggregateItem:
    return AggregateItem(
        benchmark_id=benchmark_id,
        benchmark_name=benchmark_name,
        ability=ability,
        language=language,
        final_score=Decimal(score),
        answer_score=Decimal(score),
        citation_score=None,
        requires_citations=False,
        succeeded=succeeded,
        call_count=calls,
    )


def test_aggregation_weights_questions_equally_within_each_ability() -> None:
    reasoning_large = uuid4()
    reasoning_small = uuid4()
    retrieval = uuid4()
    data_reasoning = uuid4()
    items = [
        item(reasoning_large, "Reasoning Large", Ability.REASONING, 100)
        for _ in range(3)
    ]
    items.extend(
        [
            item(reasoning_small, "Reasoning Small", Ability.REASONING, 0),
            item(retrieval, "Retrieval", Ability.RETRIEVAL, 80, language=Language.EN),
            item(
                data_reasoning,
                "Data",
                Ability.DATA_REASONING,
                60,
                succeeded=False,
                calls=2,
            ),
        ]
    )

    aggregate = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=items,
        ability_weights={
            Ability.REASONING: 40,
            Ability.RETRIEVAL: 35,
            Ability.DATA_REASONING: 25,
        },
    )

    assert aggregate.ability_scores[Ability.REASONING] == Decimal("75")
    assert aggregate.ability_scores[Ability.RETRIEVAL] == Decimal("80")
    assert aggregate.ability_scores[Ability.DATA_REASONING] == Decimal("60")
    assert aggregate.overall_score == Decimal("73")
    assert aggregate.language_scores[Language.EN] == Decimal("80")
    assert aggregate.answer_quality.quantize(Decimal("0.01")) == Decimal("73.33")
    assert aggregate.success_rate.quantize(Decimal("0.01")) == Decimal("83.33")
    assert aggregate.anomaly_rate.quantize(Decimal("0.01")) == Decimal("16.67")
    assert aggregate.http_call_count == 7


def test_equal_scores_share_rank_and_call_count_does_not_break_tie() -> None:
    base = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent B",
        provider="Provider",
        version=1,
        items=[item(uuid4(), "Reasoning", Ability.REASONING, 50, calls=20)],
        ability_weights={
            Ability.REASONING: 100,
            Ability.RETRIEVAL: 0,
            Ability.DATA_REASONING: 0,
        },
    )
    assert set(base.ability_scores) == {Ability.REASONING}
    tied = replace(
        base,
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent A",
        http_call_count=1,
    )
    lower = replace(
        base,
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent C",
        overall_score=Decimal("49"),
    )

    rankings = build_rankings([base, lower, tied])

    assert [ranking["agent_name"] for ranking in rankings] == ["Agent A", "Agent B", "Agent C"]
    assert [ranking["rank"] for ranking in rankings] == [1, 1, 3]
    assert [ranking["http_call_count"] for ranking in rankings[:2]] == [1, 20]


def test_uncovered_dimensions_are_excluded_instead_of_scored_as_zero() -> None:
    aggregate = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=[item(uuid4(), "Instruction", Ability.INSTRUCTION_FOLLOWING, 80)],
        ability_weights={ability: 20 for ability in Ability},
    )

    assert aggregate.overall_score == Decimal("80")
    assert aggregate.ability_scores == {Ability.INSTRUCTION_FOLLOWING: Decimal("80")}


def test_attempt_metrics_sum_retries_and_use_nearest_rank_percentiles() -> None:
    aggregate_item = item(
        uuid4(), "Reasoning", Ability.REASONING, 80, calls=3
    )
    aggregate_item = replace(
        aggregate_item,
        attempts=(
            AggregateAttempt(10, 4, 2, 6),
            AggregateAttempt(20, 5, 3, 8),
            AggregateAttempt(None, 7, None, 9),
        ),
    )

    aggregate = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=[aggregate_item],
        ability_weights={
            ability: 100 if ability == Ability.REASONING else 0 for ability in Ability
        },
    )

    assert aggregate.total_input_tokens == 16
    assert aggregate.total_output_tokens == 5
    assert aggregate.total_tokens == 23
    assert aggregate.token_usage_available_count == 2
    assert aggregate.token_usage_missing_count == 1
    assert aggregate.latency_p50_ms == 10
    assert aggregate.latency_p95_ms == 20
    assert aggregate.latency_sample_count == 2
