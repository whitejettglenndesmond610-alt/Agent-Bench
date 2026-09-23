from collections import Counter
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from agent_eval.datasets.audit import (
    BALANCED_V2_DIFFICULTY_QUOTA,
    BALANCED_V2_LANGUAGE_QUOTA,
    BALANCED_V2_QUESTIONS_PER_DIMENSION,
)
from agent_eval.datasets.importer import (
    BALANCED_V2_COHORT_KEY,
    HELDOUT_V1_COHORT_KEY,
    LEGACY_COHORT_KEY,
    OFFICIAL_V4_COHORT_KEY,
    TAU2_V1_COHORT_KEY,
)
from agent_eval.db.enums import RELEASED_ABILITIES, Ability, DatasetStatus, RunScope
from agent_eval.db.models import DatasetVersion, Question, ScoreConfig
from agent_eval.scoring.configs import (
    BALANCED_V2_ABILITY_WEIGHTS,
    LEGACY_ABILITY_WEIGHTS,
    ability_weights,
    has_ability_weights,
)

DEFAULT_PROFILE_KEY = "comprehensive"

# Ranking metrics for weight-aggregated profiles; the agentic ability (tau2
# interactive runs) is never part of weighted ranking aggregation.
WEIGHTED_PUBLISH_METRICS = [
    "overall",
    *(ability.value for ability in BALANCED_V2_ABILITY_WEIGHTS),
]
BALANCED_V2_PROFILE_KEY = "balanced-v2"
UNIFIED_V3_PROFILE_KEY = "unified-v3"
UNIFIED_V3_EXPECTED_COUNTS = {
    Ability.REASONING: 45,
    Ability.RETRIEVAL: 35,
    Ability.DATA_REASONING: 35,
    Ability.INSTRUCTION_FOLLOWING: 30,
    Ability.LONG_CONTEXT: 30,
}
UNIFIED_V3_SPECIALIST_PROFILE_KEYS = {
    ability: f"{UNIFIED_V3_PROFILE_KEY}-{ability.value}"
    for ability in UNIFIED_V3_EXPECTED_COUNTS
}
UNIFIED_V3_PROFILE_KEYS = frozenset(
    {UNIFIED_V3_PROFILE_KEY, *UNIFIED_V3_SPECIALIST_PROFILE_KEYS.values()}
)
OFFICIAL_V4_PROFILE_KEY = "official-v4"
OFFICIAL_V4_LEGACY_RETRIEVAL = {"BrowseComp", "BrowseComp-ZH"}
OFFICIAL_V4_LEGACY_DATA = {"FinQA"}
OFFICIAL_V4_EXPECTED_NEW = {"GPQA-Diamond": 20, "CMMLU": 20, "GSM8K": 20}
OFFICIAL_V4_EXPECTED_LEGACY = {"BrowseComp": 10, "BrowseComp-ZH": 20, "FinQA": 10}
HELDOUT_V1_PROFILE_KEY = "heldout-v1"
HELDOUT_V1_EXPECTED = {
    "LiveBench-Math": 20,
    "LiveBench-Reasoning": 20,
    "LiveBench-DataAnalysis": 20,
    "HLE-TextOnly": 40,
}
TAU2_RETAIL_PROFILE_KEY = "tau2-retail"
TAU2_RETAIL_EXPECTED = {"Tau2-Retail": 5}

# The first balanced-v2 release used these two names before the Original prefix
# convention was applied consistently. Their source is still the frozen original cohort.
_LEGACY_BALANCED_ORIGINAL_NAMES = {
    "BalancedV2-Instruction-Following",
    "BalancedV2-Long-Context",
}


@dataclass(frozen=True)
class RunProfile:
    key: str
    name: str
    description: str
    scope: RunScope
    target_ability: Ability | None
    dataset_version_ids: tuple[UUID, ...]
    question_count: int
    benchmark_count: int
    publishes_to: tuple[str, ...]
    ranking_eligible: bool
    score_config_id: UUID | None
    ability_weights: dict[Ability, int]


async def build_run_profiles(session: AsyncSession) -> list[RunProfile]:
    versions = list(
        await session.scalars(
            select(DatasetVersion)
            .options(joinedload(DatasetVersion.benchmark))
            .where(
                DatasetVersion.status == DatasetStatus.FROZEN,
                DatasetVersion.run_eligible.is_(True),
            )
            .order_by(DatasetVersion.benchmark_id, DatasetVersion.version.desc())
        )
    )
    score_configs = list(await session.scalars(select(ScoreConfig).order_by(ScoreConfig.id)))
    legacy_score = next(
        (item for item in score_configs if has_ability_weights(item, LEGACY_ABILITY_WEIGHTS)),
        None,
    )
    balanced_score = next(
        (
            item
            for item in score_configs
            if has_ability_weights(item, BALANCED_V2_ABILITY_WEIGHTS)
        ),
        None,
    )

    latest_by_benchmark: dict[tuple[str, UUID], DatasetVersion] = {}
    for version in versions:
        latest_by_benchmark.setdefault((version.cohort_key, version.benchmark_id), version)
    legacy_datasets = sorted(
        (
            item
            for (cohort_key, _), item in latest_by_benchmark.items()
            if cohort_key == LEGACY_COHORT_KEY
        ),
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )

    present_abilities = [
        ability
        for ability in Ability
        if any(item.benchmark.ability == ability for item in legacy_datasets)
    ]
    profiles = [
        _profile(
            key=DEFAULT_PROFILE_KEY,
            name="Comprehensive",
            description="All latest frozen benchmark datasets.",
            scope=RunScope.COMPREHENSIVE,
            target_ability=None,
            datasets=legacy_datasets,
            publishes_to=["overall", *(ability.value for ability in present_abilities)],
            ranking_eligible=True,
            score_config=legacy_score,
        )
    ]
    specialist_names = {
        Ability.REASONING: "Reasoning Specialist",
        Ability.RETRIEVAL: "Retrieval Specialist",
        Ability.DATA_REASONING: "Data Reasoning Specialist",
        Ability.INSTRUCTION_FOLLOWING: "Instruction Following Specialist",
        Ability.LONG_CONTEXT: "Long Context Specialist",
    }
    for ability in Ability:
        if ability not in RELEASED_ABILITIES:
            continue
        profiles.append(
            _profile(
                key=ability.value,
                name=specialist_names[ability],
                description=f"Latest frozen {ability.value} benchmark datasets.",
                scope=RunScope.SPECIALIST,
                target_ability=ability,
                datasets=[
                    item for item in legacy_datasets if item.benchmark.ability == ability
                ],
                publishes_to=[ability.value],
                ranking_eligible=True,
                score_config=legacy_score,
            )
        )

    smoke_names = {"MMLU-Pro", "FinQA"}
    smoke_datasets = [item for item in legacy_datasets if item.benchmark.name in smoke_names]
    if {item.benchmark.name for item in smoke_datasets} == smoke_names:
        profiles.append(
            _profile(
                key="smoke",
                name="Smoke",
                description="Small MMLU-Pro and FinQA validation bundle.",
                scope=RunScope.SMOKE,
                target_ability=None,
                datasets=smoke_datasets,
                publishes_to=[],
                ranking_eligible=False,
                score_config=legacy_score,
            )
        )

    balanced_datasets = sorted(
        (
            item
            for (cohort_key, _), item in latest_by_benchmark.items()
            if cohort_key == BALANCED_V2_COHORT_KEY
        ),
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    if balanced_score is not None and await _stored_balanced_v2_is_complete(
        session, balanced_datasets
    ):
        profiles.append(
            _profile(
                key=BALANCED_V2_PROFILE_KEY,
                name="Balanced V2 Comprehensive",
                description="Exact frozen 150-question balanced-v2 cohort.",
                scope=RunScope.COMPREHENSIVE,
                target_ability=None,
                datasets=balanced_datasets,
                publishes_to=list(WEIGHTED_PUBLISH_METRICS),
                ranking_eligible=True,
                score_config=balanced_score,
            )
        )
    balanced_originals = [
        item for item in balanced_datasets if _is_balanced_v2_original(item)
    ]
    unified_datasets = sorted(
        [*legacy_datasets, *balanced_originals],
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    official_new = sorted(
        (
            item
            for (cohort_key, _), item in latest_by_benchmark.items()
            if cohort_key == OFFICIAL_V4_COHORT_KEY
        ),
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    official_legacy = [
        item
        for item in legacy_datasets
        if item.benchmark.name in OFFICIAL_V4_LEGACY_RETRIEVAL | OFFICIAL_V4_LEGACY_DATA
    ]
    official_datasets = sorted(
        [*official_new, *official_legacy],
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    official_ready = await _stored_official_v4_is_complete(session, official_datasets)

    if balanced_score is not None and await _stored_unified_v3_is_complete(
        session, unified_datasets
    ):
        profiles = [
            replace(profile, publishes_to=(), ranking_eligible=False)
            if profile.key != "smoke"
            else profile
            for profile in profiles
        ]
        profiles.append(
            _profile(
                key=UNIFIED_V3_PROFILE_KEY,
                name="Unified V3 Comprehensive",
                description="Immutable 175-question legacy plus balanced-original profile.",
                scope=RunScope.COMPREHENSIVE,
                target_ability=None,
                datasets=unified_datasets,
                publishes_to=list(WEIGHTED_PUBLISH_METRICS),
                ranking_eligible=True,
                score_config=balanced_score,
            )
        )
        for ability in UNIFIED_V3_EXPECTED_COUNTS:
            profiles.append(
                _profile(
                    key=UNIFIED_V3_SPECIALIST_PROFILE_KEYS[ability],
                    name=f"Unified V3 {specialist_names[ability]}",
                    description=f"Unified-v3 {ability.value} question subset.",
                    scope=RunScope.SPECIALIST,
                    target_ability=ability,
                    datasets=[
                        item
                        for item in unified_datasets
                        if item.benchmark.ability == ability
                    ],
                    publishes_to=[ability.value],
                    ranking_eligible=True,
                    score_config=balanced_score,
                )
            )
    if official_ready:
        official_abilities = [
            ability
            for ability in (Ability.REASONING, Ability.RETRIEVAL, Ability.DATA_REASONING)
            if any(item.benchmark.ability == ability for item in official_datasets)
        ]
        profiles.append(
            _profile(
                key=OFFICIAL_V4_PROFILE_KEY,
                name="Official V4 Comprehensive",
                description="GPQA Diamond, CMMLU, GSM8K plus BrowseComp and FinQA.",
                scope=RunScope.COMPREHENSIVE,
                target_ability=None,
                datasets=official_datasets,
                publishes_to=["overall", *(ability.value for ability in official_abilities)],
                ranking_eligible=True,
                score_config=legacy_score,
            )
        )
    heldout_datasets = sorted(
        (
            item
            for (cohort_key, _), item in latest_by_benchmark.items()
            if cohort_key == HELDOUT_V1_COHORT_KEY
        ),
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    if await _stored_heldout_v1_is_complete(session, heldout_datasets):
        heldout_abilities = [
            ability
            for ability in (Ability.REASONING, Ability.RETRIEVAL, Ability.DATA_REASONING)
            if any(item.benchmark.ability == ability for item in heldout_datasets)
        ]
        profiles.append(
            _profile(
                key=HELDOUT_V1_PROFILE_KEY,
                name="Held-out V1 Comprehensive",
                description="Private LiveBench and HLE held-out cohort with masked question text.",
                scope=RunScope.COMPREHENSIVE,
                target_ability=None,
                datasets=heldout_datasets,
                publishes_to=["overall", *(ability.value for ability in heldout_abilities)],
                ranking_eligible=True,
                score_config=legacy_score,
            )
        )
    tau2_datasets = sorted(
        (
            item
            for (cohort_key, _), item in latest_by_benchmark.items()
            if cohort_key == TAU2_V1_COHORT_KEY
        ),
        key=lambda item: (item.benchmark.name.casefold(), str(item.id)),
    )
    if is_complete_tau2_retail(tau2_datasets):
        profiles.append(
            _profile(
                key=TAU2_RETAIL_PROFILE_KEY,
                name="Tau2 Retail Interactive",
                description="Five tau2-bench retail interactive tasks against HTTP agents.",
                scope=RunScope.SMOKE,
                target_ability=None,
                datasets=tau2_datasets,
                publishes_to=[],
                ranking_eligible=False,
                score_config=None,
            )
        )
    return profiles


def default_profile_key(profiles: list[RunProfile]) -> str:
    if any(profile.key == OFFICIAL_V4_PROFILE_KEY for profile in profiles):
        return OFFICIAL_V4_PROFILE_KEY
    if any(profile.key == UNIFIED_V3_PROFILE_KEY for profile in profiles):
        return UNIFIED_V3_PROFILE_KEY
    return DEFAULT_PROFILE_KEY


def available_run_profiles(profiles: list[RunProfile]) -> list[RunProfile]:
    allowed = {
        "smoke",
        OFFICIAL_V4_PROFILE_KEY,
        HELDOUT_V1_PROFILE_KEY,
        TAU2_RETAIL_PROFILE_KEY,
        *UNIFIED_V3_PROFILE_KEYS,
    }
    # smoke and tau2-retail alone must not hide the legacy profile list.
    standalone = {"smoke", TAU2_RETAIL_PROFILE_KEY}
    if any(profile.key in allowed and profile.key not in standalone for profile in profiles):
        return [profile for profile in profiles if profile.key in allowed]
    return profiles


async def _stored_balanced_v2_is_complete(
    session: AsyncSession, datasets: list[DatasetVersion]
) -> bool:
    if sum(item.question_count for item in datasets) != 150 or not datasets:
        return False
    questions = list(
        await session.scalars(
            select(Question).where(
                Question.dataset_version_id.in_(item.id for item in datasets)
            )
        )
    )
    return is_complete_balanced_v2(datasets, questions)


def is_complete_balanced_v2(
    datasets: list[DatasetVersion], questions: list[Question]
) -> bool:
    if sum(item.question_count for item in datasets) != 150 or len(questions) != 150:
        return False
    datasets_by_id = {item.id: item for item in datasets}
    ability_counts: Counter[Ability] = Counter()
    language_counts: Counter[tuple[Ability, str]] = Counter()
    difficulty_counts: Counter[tuple[Ability, str | None]] = Counter()
    for question in questions:
        benchmark = datasets_by_id[question.dataset_version_id].benchmark
        ability_counts[benchmark.ability] += 1
        language_counts[(benchmark.ability, benchmark.language.value)] += 1
        difficulty_counts[(benchmark.ability, question.difficulty)] += 1
    return all(
        ability_counts[ability] == BALANCED_V2_QUESTIONS_PER_DIMENSION
        and all(
            language_counts[(ability, language)] == expected
            for language, expected in BALANCED_V2_LANGUAGE_QUOTA.items()
        )
        and all(
            difficulty_counts[(ability, difficulty)] == expected
            for difficulty, expected in BALANCED_V2_DIFFICULTY_QUOTA.items()
        )
        for ability in BALANCED_V2_ABILITY_WEIGHTS
    )


async def _stored_unified_v3_is_complete(
    session: AsyncSession, datasets: list[DatasetVersion]
) -> bool:
    if sum(item.question_count for item in datasets) != 175 or not datasets:
        return False
    questions = list(
        await session.scalars(
            select(Question).where(
                Question.dataset_version_id.in_(item.id for item in datasets)
            )
        )
    )
    return is_complete_unified_v3(datasets, questions)


def is_complete_unified_v3(
    datasets: list[DatasetVersion], questions: list[Question]
) -> bool:
    legacy = [item for item in datasets if item.cohort_key == LEGACY_COHORT_KEY]
    originals = [item for item in datasets if _is_balanced_v2_original(item)]
    if (
        len(legacy) != 6
        or len(originals) != 7
        or len(datasets) != 13
        or sum(item.question_count for item in legacy) != 100
        or sum(item.question_count for item in originals) != 75
        or len(questions) != 175
    ):
        return False
    dataset_ids = {item.id for item in datasets}
    if {question.dataset_version_id for question in questions} - dataset_ids:
        return False
    stored_counts = Counter(question.dataset_version_id for question in questions)
    if any(stored_counts[item.id] != item.question_count for item in datasets):
        return False
    actual_counts = Counter(
        next(
            item.benchmark.ability
            for item in datasets
            if item.id == question.dataset_version_id
        )
        for question in questions
    )
    normalized_prompts = [" ".join(question.prompt.split()).casefold() for question in questions]
    return (
        actual_counts == Counter(UNIFIED_V3_EXPECTED_COUNTS)
        and len(normalized_prompts) == len(set(normalized_prompts))
    )


async def _stored_official_v4_is_complete(
    session: AsyncSession, datasets: list[DatasetVersion]
) -> bool:
    if not is_complete_official_v4(datasets):
        return False
    questions = list(
        await session.scalars(
            select(Question).where(Question.dataset_version_id.in_(item.id for item in datasets))
        )
    )
    return len(questions) == sum(item.question_count for item in datasets)


def is_complete_official_v4(datasets: list[DatasetVersion]) -> bool:
    by_name = {item.benchmark.name: item for item in datasets}
    expected = {**OFFICIAL_V4_EXPECTED_NEW, **OFFICIAL_V4_EXPECTED_LEGACY}
    if set(by_name) != set(expected):
        return False
    return all(by_name[name].question_count == count for name, count in expected.items())


async def _stored_heldout_v1_is_complete(
    session: AsyncSession, datasets: list[DatasetVersion]
) -> bool:
    if not is_complete_heldout_v1(datasets):
        return False
    questions = list(
        await session.scalars(
            select(Question).where(Question.dataset_version_id.in_(item.id for item in datasets))
        )
    )
    return len(questions) == sum(item.question_count for item in datasets)


def is_complete_heldout_v1(datasets: list[DatasetVersion]) -> bool:
    if any(not item.held_out for item in datasets):
        return False
    by_name = {item.benchmark.name: item for item in datasets}
    if set(by_name) != set(HELDOUT_V1_EXPECTED):
        return False
    return all(
        by_name[name].question_count == count for name, count in HELDOUT_V1_EXPECTED.items()
    )


def is_complete_tau2_retail(datasets: list[DatasetVersion]) -> bool:
    if not datasets or any(item.held_out for item in datasets):
        return False
    by_name = {item.benchmark.name: item for item in datasets}
    if set(by_name) != set(TAU2_RETAIL_EXPECTED):
        return False
    return all(
        by_name[name].question_count == count
        for name, count in TAU2_RETAIL_EXPECTED.items()
    )


def _is_balanced_v2_original(dataset: DatasetVersion) -> bool:
    return dataset.cohort_key == BALANCED_V2_COHORT_KEY and (
        dataset.benchmark.name.startswith("BalancedV2-Original-")
        or dataset.benchmark.name in _LEGACY_BALANCED_ORIGINAL_NAMES
    )


def _profile(
    *,
    key: str,
    name: str,
    description: str,
    scope: RunScope,
    target_ability: Ability | None,
    datasets: list[DatasetVersion],
    publishes_to: list[str],
    ranking_eligible: bool,
    score_config: ScoreConfig | None,
) -> RunProfile:
    return RunProfile(
        key=key,
        name=name,
        description=description,
        scope=scope,
        target_ability=target_ability,
        dataset_version_ids=tuple(item.id for item in datasets),
        question_count=sum(item.question_count for item in datasets),
        benchmark_count=len(datasets),
        publishes_to=tuple(publishes_to),
        ranking_eligible=ranking_eligible,
        score_config_id=score_config.id if score_config is not None else None,
        ability_weights=(
            ability_weights(score_config) if score_config is not None else LEGACY_ABILITY_WEIGHTS
        ),
    )
