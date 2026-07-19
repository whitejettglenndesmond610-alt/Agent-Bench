from pydantic import BaseModel, Field

from agent_eval.db.enums import Ability, Language


class DashboardProfileSummary(BaseModel):
    key: str
    name: str
    ready: bool
    question_count: int = Field(ge=0)


class DashboardActiveProfile(DashboardProfileSummary):
    dataset_version_count: int = Field(ge=0)
    language_counts: dict[Language, int]
    ability_counts: dict[Ability, int]
    ability_weights: dict[Ability, int]


class DashboardSummary(BaseModel):
    active_profile: DashboardActiveProfile
    ranking_profiles: list[DashboardProfileSummary]
    judge_model: str | None
