from dataclasses import dataclass

from agent_eval.agents.prompt import build_question_prompt
from agent_eval.db.models import AgentVersion, Question


@dataclass(frozen=True)
class ProfileCompatibility:
    profile_key: str
    compatible: bool
    oversized_question_count: int
    max_question_characters: int
    citation_required_count: int
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def analyze_profile_compatibility(
    version: AgentVersion,
    profile_key: str,
    questions: list[Question],
) -> ProfileCompatibility:
    prompt_lengths = [
        len(
            build_question_prompt(
                question_type=question.type,
                prompt=question.prompt,
                choices=question.choices_json,
                requires_citations=question.requires_citations,
                ability=question.dataset_version.benchmark.ability,
            )
        )
        for question in questions
    ]
    maximum = max(prompt_lengths, default=0)
    oversized = (
        sum(length > version.max_prompt_characters for length in prompt_lengths)
        if version.max_prompt_characters is not None
        else 0
    )
    citations_required = sum(question.requires_citations for question in questions)
    return ProfileCompatibility(
        profile_key=profile_key,
        compatible=True,
        oversized_question_count=oversized,
        max_question_characters=maximum,
        citation_required_count=citations_required,
        blocking_reasons=(),
        warnings=(),
    )
