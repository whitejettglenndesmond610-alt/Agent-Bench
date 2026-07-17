from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from uuid import UUID

from agent_eval.db.enums import Ability, Language, ResultValidityStatus

DISPLAY_QUANTUM = Decimal("0.01")
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
) -> AgentAggregate:
    if not items:
        raise ValueError("cannot aggregate an agent without execution items")

    by_benchmark: dict[UUID, list[AggregateItem]] = defaultdict(list)
    for item in items:
        by_benchmark[item.benchmark_id].append(item)
    benchmarks = [
        BenchmarkAggregate(
            benchmark_id=benchmark_id,
            name=benchmark_items[0].benchmark_name,
            ability=benchmark_items[0].ability,
            language=benchmark_items[0].language,
            score=mean([item.final_score for item in benchmark_items]),
        )
        for benchmark_id, benchmark_items in by_benchmark.items()
    ]
    benchmarks.sort(key=lambda item: (item.name.casefold(), str(item.benchmark_id)))

    ability_scores = {
        ability: mean([item.final_score for item in items if item.ability == ability])
        for ability in Ability
        if any(item.ability == ability for item in items)
    }
    language_scores = {
        language: mean([item.final_score for item in items if item.language == language])
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
        total_input_tokens=sum_optional(attempt.input_tokens for attempt in attempts),
        total_output_tokens=sum_optional(attempt.output_tokens for attempt in attempts),
        total_tokens=sum_optional(attempt.total_tokens for attempt in attempts),
        token_usage_available_count=complete_usage,
        token_usage_missing_count=len(attempts) - complete_usage,
        latency_p50_ms=nearest_rank(latencies, 50),
        latency_p95_ms=nearest_rank(latencies, 95),
        latency_sample_count=len(latencies),
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
    }


def display(value: Decimal) -> float:
    return float(value.quantize(DISPLAY_QUANTUM, rounding=ROUND_HALF_UP))


def sum_optional(values: Iterable[int | None]) -> int | None:
    observed = [value for value in values if isinstance(value, int)]
    return sum(observed) if observed else None


def nearest_rank(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    return values[ceil(percentile * len(values) / 100) - 1]
