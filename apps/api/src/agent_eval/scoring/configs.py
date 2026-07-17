from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.db.enums import Ability
from agent_eval.db.models import ScoreConfig

LEGACY_SCORE_NAME = "MVP Default 40-35-25"
BALANCED_V2_SCORE_NAME = "Balanced V2 20 Each"

LEGACY_ABILITY_WEIGHTS = {
    Ability.REASONING: 40,
    Ability.RETRIEVAL: 35,
    Ability.DATA_REASONING: 25,
    Ability.INSTRUCTION_FOLLOWING: 0,
    Ability.LONG_CONTEXT: 0,
}
BALANCED_V2_ABILITY_WEIGHTS = {ability: 20 for ability in Ability}


def ability_weights(score_config: ScoreConfig) -> dict[Ability, int]:
    return {
        Ability.REASONING: score_config.reasoning_weight,
        Ability.RETRIEVAL: score_config.retrieval_weight,
        Ability.DATA_REASONING: score_config.data_weight,
        Ability.INSTRUCTION_FOLLOWING: score_config.instruction_following_weight,
        Ability.LONG_CONTEXT: score_config.long_context_weight,
    }


def has_ability_weights(score_config: ScoreConfig, expected: dict[Ability, int]) -> bool:
    return ability_weights(score_config) == expected


async def ensure_balanced_v2_score_config(session: AsyncSession) -> ScoreConfig:
    score = await session.scalar(
        select(ScoreConfig).where(ScoreConfig.name == BALANCED_V2_SCORE_NAME)
    )
    if score is None:
        score = ScoreConfig(
            name=BALANCED_V2_SCORE_NAME,
            reasoning_weight=20,
            retrieval_weight=20,
            data_weight=20,
            instruction_following_weight=20,
            long_context_weight=20,
        )
        session.add(score)
        await session.flush()
    elif not has_ability_weights(score, BALANCED_V2_ABILITY_WEIGHTS):
        raise RuntimeError("balanced-v2 score configuration has unexpected weights")
    return score
