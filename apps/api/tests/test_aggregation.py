from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

from agent_eval.db.enums import Ability, Language
from agent_eval.scoring.aggregation import (
    AgentAggregate,
    AggregateAttempt,
    AggregateItem,
    aggregate_agent,
    build_rankings,
    serialize_aggregate,
)
from agent_eval.scoring.pricing import ModelPricing


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
        ability_weights={ability: 20 for ability in Ability if ability is not Ability.AGENTIC},
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


def trial_item(
    question_id: UUID,
    trial_index: int,
    benchmark_id: UUID,
    benchmark_name: str,
    ability: Ability,
    score: int,
    *,
    language: Language = Language.ZH,
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
        succeeded=True,
        call_count=1,
        question_id=question_id,
        trial_index=trial_index,
    )


def test_trial_scores_are_averaged_per_question_before_scoring() -> None:
    reasoning = uuid4()
    retrieval = uuid4()
    question_one = uuid4()
    question_two = uuid4()
    question_three = uuid4()
    items = [
        trial_item(question_one, 0, reasoning, "Reasoning", Ability.REASONING, 100),
        trial_item(question_one, 1, reasoning, "Reasoning", Ability.REASONING, 0),
        trial_item(question_two, 0, reasoning, "Reasoning", Ability.REASONING, 100),
        trial_item(question_two, 1, reasoning, "Reasoning", Ability.REASONING, 100),
        trial_item(question_three, 0, retrieval, "Retrieval", Ability.RETRIEVAL, 0),
        trial_item(question_three, 1, retrieval, "Retrieval", Ability.RETRIEVAL, 100),
    ]

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

    # question means are 50, 100 and 50
    assert aggregate.ability_scores[Ability.REASONING] == Decimal("75")
    assert aggregate.ability_scores[Ability.RETRIEVAL] == Decimal("50")
    assert aggregate.overall_score.quantize(Decimal("0.01")) == Decimal("63.33")
    assert aggregate.language_scores[Language.ZH].quantize(Decimal("0.01")) == Decimal("66.67")
    assert aggregate.pass_at_1.quantize(Decimal("0.01")) == Decimal("66.67")
    assert aggregate.pass_at_k == Decimal("100")
    assert aggregate.pass_pow_k.quantize(Decimal("0.01")) == Decimal("33.33")
    assert aggregate.ci_low is not None and aggregate.ci_high is not None
    assert aggregate.ci_low <= aggregate.ci_high


def test_single_trial_grouping_matches_legacy_per_item_semantics() -> None:
    benchmark_id = uuid4()
    grouped_items = [
        trial_item(uuid4(), 0, benchmark_id, "Reasoning", Ability.REASONING, 100),
        trial_item(uuid4(), 0, benchmark_id, "Reasoning", Ability.REASONING, 0),
        trial_item(uuid4(), 0, benchmark_id, "Reasoning", Ability.REASONING, 50),
    ]
    legacy_items = [
        item(benchmark_id, "Reasoning", Ability.REASONING, 100),
        item(benchmark_id, "Reasoning", Ability.REASONING, 0),
        item(benchmark_id, "Reasoning", Ability.REASONING, 50),
    ]
    weights = {ability: 100 if ability == Ability.REASONING else 0 for ability in Ability}

    grouped = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=grouped_items,
        ability_weights=weights,
    )
    legacy = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=legacy_items,
        ability_weights=weights,
    )

    assert grouped.overall_score == legacy.overall_score
    assert grouped.ability_scores == legacy.ability_scores
    assert grouped.pass_at_1 == grouped.pass_at_k == grouped.pass_pow_k
    assert grouped.pass_at_1.quantize(Decimal("0.01")) == Decimal("33.33")


def aggregate_with_pricing(
    items: list[AggregateItem], pricing: ModelPricing | None
) -> AgentAggregate:
    return aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=items,
        ability_weights={
            ability: 100 if ability == Ability.REASONING else 0 for ability in Ability
        },
        pricing=pricing,
    )


def test_cost_aggregation_prices_attempts_with_complete_usage() -> None:
    priced_item = replace(
        item(uuid4(), "Reasoning", Ability.REASONING, 80),
        attempts=(
            AggregateAttempt(10, 1_000_000, 500_000, 1_500_000),
            AggregateAttempt(20, None, None, None),
        ),
    )
    plain_item = item(uuid4(), "Reasoning", Ability.REASONING, 60)
    pricing = ModelPricing(input_per_mtok=Decimal("2"), output_per_mtok=Decimal("10"))

    aggregate = aggregate_with_pricing([priced_item, plain_item], pricing)

    assert aggregate.total_cost_usd == Decimal("7")
    assert aggregate.cost_per_task_usd == Decimal("3.5")
    assert aggregate.input_token_share is not None
    assert aggregate.output_token_share is not None
    assert aggregate.input_token_share.quantize(Decimal("0.01")) == Decimal("66.67")
    assert aggregate.output_token_share.quantize(Decimal("0.01")) == Decimal("33.33")


def test_cost_aggregation_is_none_without_pricing_or_usage() -> None:
    usage_item = replace(
        item(uuid4(), "Reasoning", Ability.REASONING, 80),
        attempts=(AggregateAttempt(10, 100, 50, 150),),
    )

    unpriced = aggregate_with_pricing([usage_item], None)
    assert unpriced.total_cost_usd is None
    assert unpriced.cost_per_task_usd is None
    assert unpriced.input_token_share is not None

    pricing = ModelPricing(input_per_mtok=Decimal("2"), output_per_mtok=Decimal("10"))
    no_usage = aggregate_with_pricing(
        [item(uuid4(), "Reasoning", Ability.REASONING, 80)], pricing
    )
    assert no_usage.total_cost_usd is None
    assert no_usage.cost_per_task_usd is None
    assert no_usage.input_token_share is None
    assert no_usage.output_token_share is None

def test_simulator_tokens_stay_out_of_cost_and_reach_run_metadata() -> None:
    usage_item = replace(
        item(uuid4(), "Tau2-Retail", Ability.AGENTIC, 100),
        attempts=(AggregateAttempt(10, 1_000, 200, 1_200),),
    )
    pricing = ModelPricing(input_per_mtok=Decimal("2"), output_per_mtok=Decimal("10"))

    aggregate = aggregate_agent(
        run_agent_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        agent_name="Agent",
        provider="Provider",
        version=1,
        items=[usage_item],
        ability_weights={Ability.AGENTIC: 100},
        pricing=pricing,
        simulator_input_tokens=40_000,
        simulator_output_tokens=8_000,
    )
    # cost reflects only the tested agent's tokens
    assert aggregate.total_cost_usd == Decimal("0.004")
    assert aggregate.simulator_input_tokens == 40_000
    assert aggregate.simulator_output_tokens == 8_000

    serialized = serialize_aggregate(aggregate, 1)
    assert serialized["simulator_input_tokens"] == 40_000
    assert serialized["simulator_output_tokens"] == 8_000

    plain = aggregate_with_pricing([item(uuid4(), "Reasoning", Ability.REASONING, 80)], pricing)
    assert plain.simulator_input_tokens is None
    assert plain.simulator_output_tokens is None
    assert serialize_aggregate(plain, 1)["simulator_input_tokens"] is None

