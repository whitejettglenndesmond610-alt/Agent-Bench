from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceExperienceTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=20_000)
    category: Literal[
        "analysis",
        "retrieval_citations",
        "structured_extraction",
        "writing_instruction_following",
        "data_reasoning",
        "long_context_synthesis",
    ]
    difficulty: Literal["beginner", "intermediate", "advanced"]
    recommended_mode: Literal["chat", "comparison"]
    min_agents: int = Field(ge=1, le=5)
    max_agents: int = Field(ge=1, le=5)
    ranking_eligible: Literal[False] = False
