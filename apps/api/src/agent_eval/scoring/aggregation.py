from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from uuid import UUID

from agent_eval.db.enums import Ability, Language, ResultValidityStatus
from agent_eval.scoring.pricing import ModelPricing, usage_cost_usd
from agent_eval.scoring.statistics import (
    QuestionReliability,
    pass_at_1,
    pass_at_k,
    pass_pow_k,
    stratified_bootstrap_ci,
    summarize_question,
)

DISPLAY_QUANTUM = Decimal("0.01")
COST_QUANTUM = Decimal("0.000001")
AGGREGATION_VERSION = "2"


@dataclass(frozen=True)
class AggregateItem:
    benchmark_id: UUID
    benchmark_name: str
    ability: Ability
    language: Language
    final_score: Decimal
    answer_score: Decimal
    citation_score: Decimal | None
    requires_citations: bool
    succeeded: bool
    call_count: int
    attempts: tuple["AggregateAttempt", ...] = ()
    question_id: UUID | None = None
    trial_index: int = 0


@dataclass(frozen=True)
class AggregateAttempt:
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class BenchmarkAggregate:
    benchmark_id: UUID
    name: str
    ability: Ability
    language: Language
    score: Decimal


@dataclass(frozen=True)
class AgentAggregate:
    run_agent_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    agent_name: str
    provider: str
    version: int
    overall_score: Decimal
    ability_scores: dict[Ability, Decimal]
    language_scores: dict[Language, Decimal]
    benchmark_scores: list[BenchmarkAggregate]
    answer_quality: Decimal
    citation_quality: Decimal
    success_rate: Decimal
    anomaly_rate: Decimal
    http_call_count: int
    validity_status: ResultValidityStatus
    validity_reason: str | None
    configuration_failure_count: int
    configuration_failure_rate: Decimal
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_tokens: int | None
    token_usage_available_count: int
    token_usage_missing_count: int
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_sample_count: int
    pass_at_1: Decimal
    pass_at_k: Decimal
    pass_pow_k: Decimal
    ci_low: Decimal | None
    ci_high: Decimal | None
    total_cost_usd: Decimal | None
    cost_per_task_usd: Decimal | None
    input_token_share: Decimal | None
    output_token_share: Decimal | None
    # DeepSeek user-simulator tokens (tau2 interactive runs): evaluation
    # overhead, kept out of every cost figure above.
    simulator_input_tokens: int | None = None
    simulator_output_tokens: int | None = None


@dataclass(frozen=True)
class QuestionScore:
    benchmark_id: UUID
    benchmark_name: str
    ability: Ability
    language: Language
    reliability: QuestionReliability

    @property
    def question_mean(self) -> Decimal:
        return self.reliability.question_mean


def group_question_scores(items: list[AggregateItem]) -> list[QuestionScore]:
    groups: dict[UUID | int, list[AggregateItem]] = {}
    for index, item in enumerate(items):
        key: UUID | int = item.question_id if item.question_id is not None else -(index + 1)
        groups.setdefault(key, []).append(item)
    question_scores: list[QuestionScore] = []
    for group in groups.values():
        first = group[0]
        trials = sorted(group, key=lambda item: item.trial_index)
        question_scores.append(
            QuestionScore(
                benchmark_id=first.benchmark_id,
                benchmark_name=first.benchmark_name,
                ability=first.ability,
                language=first.language,
                reliability=summarize_question([item.final_score for item in trials]),
            )
        )
    return question_scores


def mean(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    return sum(values, start=Decimal(0)) / Decimal(len(values))


def aggregate_agent(
    *,
    run_agent_id: UUID,
    agent_id: UUID,
    agent_version_id: UUID,
    agent_name: str,
    provider: str,
    version: int,
    items: list[AggregateItem],
    ability_weights: dict[Ability, int],
    validity_status: ResultValidityStatus = ResultValidityStatus.PENDING,
    validity_reason: str | None = None,
    configuration_failure_count: int = 0,
    configuration_failure_rate: Decimal = Decimal(0),
    pricing: ModelPricing | None = None,
    simulator_input_tokens: int | None = None,
    simulator_output_tokens: int | None = None,
) -> AgentAggregate:
    if not items:
        raise ValueError("cannot aggregate an agent without execution items")

    question_scores = group_question_scores(items)
    by_benchmark: dict[UUID, list[QuestionScore]] = defaultdict(list)
    for question in question_scores:
        by_benchmark[question.benchmark_id].append(question)
    benchmarks = [
        BenchmarkAggregate(
            benchmark_id=benchmark_id,
            name=benchmark_questions[0].benchmark_name,
            ability=benchmark_questions[0].ability,
            language=benchmark_questions[0].language,
            score=mean([question.question_mean for question in benchmark_questions]),
        )
        for benchmark_id, benchmark_questions in by_benchmark.items()
    ]
    benchmarks.sort(key=lambda item: (item.name.casefold(), str(item.benchmark_id)))

    ability_scores = {
        ability: mean(
            [question.question_mean for question in question_scores if question.ability == ability]
        )
        for ability in Ability
        if any(question.ability == ability for question in question_scores)
    }
    language_scores = {
        language: mean(
            [
                question.question_mean
                for question in question_scores
                if question.language == language
            ]
        )
        for language in Language
    }
    if sum(ability_weights.values()) != 100:
        raise ValueError("ability weights must total 100")
    covered_weight_total = Decimal(
        sum(ability_weights.get(ability, 0) for ability in ability_scores)
    )
    if covered_weight_total == 0:
        raise ValueError("covered abilities must have a positive configured weight")
    overall_score = sum(
        (
            score * Decimal(ability_weights.get(ability, 0))
            for ability, score in ability_scores.items()
        ),
        start=Decimal(0),
    ) / covered_weight_total
    reliabilities = [question.reliability for question in question_scores]
    questions_by_ability: dict[Ability, list[QuestionReliability]] = defaultdict(list)
    for question in question_scores:
        questions_by_ability[question.ability].append(question.reliability)
    ci_low, ci_high = stratified_bootstrap_ci(questions_by_ability, ability_weights)
    citation_scores = [
        item.citation_score or Decimal(0) for item in items if item.requires_citations
    ]
    succeeded = sum(item.succeeded for item in items)
    total = Decimal(len(items))
    attempts = [attempt for item in items for attempt in item.attempts]
    latencies = sorted(
        attempt.latency_ms for attempt in attempts if attempt.latency_ms is not None
    )
    complete_usage = sum(
        attempt.input_tokens is not None
        and attempt.output_tokens is not None
        and attempt.total_tokens is not None
        for attempt in attempts
    )
    total_input_tokens = sum_optional(attempt.input_tokens for attempt in attempts)
    total_output_tokens = sum_optional(attempt.output_tokens for attempt in attempts)
    token_sum = (total_input_tokens or 0) + (total_output_tokens or 0)
    input_token_share = (
        Decimal(total_input_tokens or 0) * 100 / token_sum if token_sum > 0 else None
    )
    total_cost_usd = cost_total(attempts, pricing)
    return AgentAggregate(
        run_agent_id=run_agent_id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        agent_name=agent_name,
        provider=provider,
        version=version,
        overall_score=overall_score,
        ability_scores=ability_scores,
        language_scores=language_scores,
        benchmark_scores=benchmarks,
        answer_quality=mean([item.answer_score for item in items]),
        citation_quality=mean(citation_scores),
        success_rate=Decimal(succeeded) * 100 / total,
        anomaly_rate=Decimal(len(items) - succeeded) * 100 / total,
        http_call_count=sum(item.call_count for item in items),
        validity_status=validity_status,
        validity_reason=validity_reason,
        configuration_failure_count=configuration_failure_count,
        configuration_failure_rate=configuration_failure_rate,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_tokens=sum_optional(attempt.total_tokens for attempt in attempts),
        token_usage_available_count=complete_usage,
        token_usage_missing_count=len(attempts) - complete_usage,
        latency_p50_ms=nearest_rank(latencies, 50),
        latency_p95_ms=nearest_rank(latencies, 95),
        latency_sample_count=len(latencies),
        pass_at_1=pass_at_1(reliabilities),
        pass_at_k=pass_at_k(reliabilities),
        pass_pow_k=pass_pow_k(reliabilities),
        ci_low=ci_low,
        ci_high=ci_high,
        total_cost_usd=total_cost_usd,
        cost_per_task_usd=(
            total_cost_usd / Decimal(len(items)) if total_cost_usd is not None else None
        ),
        input_token_share=input_token_share,
        output_token_share=(
            Decimal(100) - input_token_share if input_token_share is not None else None
        ),
        simulator_input_tokens=simulator_input_tokens,
        simulator_output_tokens=simulator_output_tokens,
    )


def build_rankings(aggregates: list[AgentAggregate]) -> list[dict[str, object]]:
    ordered = sorted(
        aggregates,
        key=lambda item: (
            -item.overall_score,
            item.agent_name.casefold(),
            str(item.agent_version_id),
        ),
    )
    rankings: list[dict[str, object]] = []
    previous_score: Decimal | None = None
    rank = 0
    for position, aggregate in enumerate(ordered, start=1):
        if previous_score is None or aggregate.overall_score != previous_score:
            rank = position
            previous_score = aggregate.overall_score
        rankings.append(serialize_aggregate(aggregate, rank))
    return rankings


def serialize_aggregate(aggregate: AgentAggregate, rank: int) -> dict[str, object]:
    return {
        "rank": rank,
        "run_agent_id": str(aggregate.run_agent_id),
        "agent_id": str(aggregate.agent_id),
        "agent_version_id": str(aggregate.agent_version_id),
        "agent_name": aggregate.agent_name,
        "provider": aggregate.provider,
        "version": aggregate.version,
        "overall_score": display(aggregate.overall_score),
        "ability_scores": {
            ability.value: display(score)
            for ability, score in aggregate.ability_scores.items()
        },
        "language_scores": {
            language.value: display(aggregate.language_scores[language]) for language in Language
        },
        "benchmark_scores": [
            {
                "benchmark_id": str(benchmark.benchmark_id),
                "name": benchmark.name,
                "ability": benchmark.ability.value,
                "language": benchmark.language.value,
                "score": display(benchmark.score),
            }
            for benchmark in aggregate.benchmark_scores
        ],
        "answer_quality": display(aggregate.answer_quality),
        "citation_quality": display(aggregate.citation_quality),
        "success_rate": display(aggregate.success_rate),
        "anomaly_rate": display(aggregate.anomaly_rate),
        "http_call_count": aggregate.http_call_count,
        "validity_status": aggregate.validity_status.value,
        "validity_reason": aggregate.validity_reason,
        "configuration_failure_count": aggregate.configuration_failure_count,
        "configuration_failure_rate": display(aggregate.configuration_failure_rate),
        "total_input_tokens": aggregate.total_input_tokens,
        "total_output_tokens": aggregate.total_output_tokens,
        "total_tokens": aggregate.total_tokens,
        "token_usage_available_count": aggregate.token_usage_available_count,
        "token_usage_missing_count": aggregate.token_usage_missing_count,
        "latency_p50_ms": aggregate.latency_p50_ms,
        "latency_p95_ms": aggregate.latency_p95_ms,
        "latency_sample_count": aggregate.latency_sample_count,
        "pass_at_1": display(aggregate.pass_at_1),
        "pass_at_k": display(aggregate.pass_at_k),
        "pass_pow_k": display(aggregate.pass_pow_k),
        "ci_low": display(aggregate.ci_low) if aggregate.ci_low is not None else None,
        "ci_high": display(aggregate.ci_high) if aggregate.ci_high is not None else None,
        "total_cost_usd": display_cost(aggregate.total_cost_usd),
        "cost_per_task_usd": display_cost(aggregate.cost_per_task_usd),
        "input_token_share": (
            display(aggregate.input_token_share)
            if aggregate.input_token_share is not None
            else None
        ),
        "output_token_share": (
            display(aggregate.output_token_share)
            if aggregate.output_token_share is not None
            else None
        ),
        "simulator_input_tokens": aggregate.simulator_input_tokens,
        "simulator_output_tokens": aggregate.simulator_output_tokens,
    }


def display(value: Decimal) -> float:
    return float(value.quantize(DISPLAY_QUANTUM, rounding=ROUND_HALF_UP))


def display_cost(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP))


def cost_total(
    attempts: list[AggregateAttempt], pricing: ModelPricing | None
) -> Decimal | None:
    if pricing is None:
        return None
    costs = [
        usage_cost_usd(pricing, attempt.input_tokens, attempt.output_tokens)
        for attempt in attempts
        if attempt.input_tokens is not None and attempt.output_tokens is not None
    ]
    return sum(costs, start=Decimal(0)) if costs else None


def sum_optional(values: Iterable[int | None]) -> int | None:
    observed = [value for value in values if isinstance(value, int)]
    return sum(observed) if observed else None


def nearest_rank(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    return values[ceil(percentile * len(values) / 100) - 1]
