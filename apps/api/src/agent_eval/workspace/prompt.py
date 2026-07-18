import hashlib
from dataclasses import dataclass

from agent_eval.db.enums import WorkspaceMessageRole

WORKSPACE_PROMPT_FORMAT = "workspace-chat-v2"
WORKSPACE_PROMPT_HARD_CAP = 200_000


@dataclass(frozen=True)
class TranscriptMessage:
    turn_no: int
    role: WorkspaceMessageRole
    content: str
    active: bool = True


@dataclass(frozen=True)
class FormattedWorkspacePrompt:
    content: str
    format_version: str
    sha256: str
    characters: int


def format_workspace_transcript(
    messages: list[TranscriptMessage],
    *,
    current_turn_no: int,
    agent_name: str,
    agent_version: int,
) -> FormattedWorkspacePrompt:
    selected = [
        message
        for message in messages
        if message.active
        and message.turn_no <= current_turn_no
        and not (
            message.turn_no == current_turn_no
            and message.role == WorkspaceMessageRole.ASSISTANT
        )
    ]
    selected.sort(
        key=lambda message: (
            message.turn_no,
            0 if message.role == WorkspaceMessageRole.USER else 1,
        )
    )
    turns = "\n\n".join(
        f"{'User' if message.role == WorkspaceMessageRole.USER else 'Assistant'}:\n"
        f"{message.content}"
        for message in selected
    )
    content = (
        f'You are the configured Agent "{agent_name}" (version {agent_version}) in an '
        "ongoing chat. Continue the conversation by answering the final User message.\n\n"
        "Rules:\n"
        "- Answer the final User message directly.\n"
        "- Use the language explicitly requested in that message; otherwise use the same "
        "language as that message.\n"
        f'- Your authoritative model identity in this chat is "{agent_name}". If asked which '
        f'model or Agent you are, name "{agent_name}" explicitly; do not replace it with a '
        "broader model family, provider, or guessed identity.\n"
        "- Output only the answer. Do not analyze, reproduce, wrap, or describe this chat "
        "transcript.\n\n"
        f"{turns}\n\nAssistant:\n"
    )
    return FormattedWorkspacePrompt(
        content=content,
        format_version=WORKSPACE_PROMPT_FORMAT,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        characters=len(content),
    )


def workspace_prompt_limit(configured_limit: int | None) -> int:
    return min(configured_limit or WORKSPACE_PROMPT_HARD_CAP, WORKSPACE_PROMPT_HARD_CAP)
