from agent_eval.db.enums import Ability, QuestionType


class PromptBuildError(ValueError):
    pass


def build_question_prompt(
    *,
    question_type: QuestionType,
    prompt: str,
    choices: list[dict[str, object]] | None,
    requires_citations: bool,
    ability: Ability | None = None,
) -> str:
    if ability == Ability.INSTRUCTION_FOLLOWING:
        if question_type != QuestionType.SHORT_ANSWER or choices or requires_citations:
            raise PromptBuildError("instruction-following questions must be exact short answers")
        return prompt
    if question_type == QuestionType.MULTIPLE_CHOICE:
        if not choices:
            raise PromptBuildError("multiple-choice question has no choices")
        rendered_choices: list[str] = []
        for choice in choices:
            label = choice.get("label")
            text = choice.get("text")
            if not isinstance(label, str) or not isinstance(text, str):
                raise PromptBuildError("multiple-choice question contains an invalid choice")
            rendered_choices.append(f"{label}. {text}")
        instruction = (
            "请只返回一个选项标识（例如 A），不要输出分析过程。 "
            "Return only one option label (for example, A) without analysis."
        )
        return f"{prompt}\n\n选项：\n" + "\n".join(rendered_choices) + f"\n\n{instruction}"

    if question_type == QuestionType.SHORT_ANSWER:
        instruction = (
            "请给出简洁、明确的最终答案，不要输出分析过程。 "
            "Give a concise final answer without analysis."
        )
    elif question_type == QuestionType.OPEN_ENDED:
        instruction = (
            "请直接给出正确、完整且相关的回答，不要输出内部分析过程。 "
            "Answer directly and completely without revealing internal reasoning."
        )
    else:
        raise PromptBuildError(f"unsupported question type: {question_type}")

    if requires_citations:
        instruction += (
            " 请同时提供支持主要事实的可访问来源链接。 "
            "Also provide accessible source links supporting the main facts."
        )
    return f"{prompt}\n\n{instruction}"
