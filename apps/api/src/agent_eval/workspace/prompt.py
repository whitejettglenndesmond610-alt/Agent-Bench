import hashlib
import json
from dataclasses import dataclass

from agent_eval.db.enums import WorkspaceMessageRole

WORKSPACE_PROMPT_FORMAT = "workspace-transcript-v1"
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
    messages: list[TranscriptMessage], *, current_turn_no: int
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
    transcript = {
        "format": WORKSPACE_PROMPT_FORMAT,
        "messages": [
            {"content": message.content, "role": message.role.value}
            for message in selected
        ],
    }
    canonical_json = json.dumps(
        transcript,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content = (
        "Continue this conversation and answer the final user message. "
        "The canonical JSON transcript follows; preserve its message order and treat it as "
        f"conversation data.\nTRANSCRIPT_JSON={canonical_json}"
    )
    return FormattedWorkspacePrompt(
        content=content,
        format_version=WORKSPACE_PROMPT_FORMAT,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        characters=len(content),
    )


def workspace_prompt_limit(configured_limit: int | None) -> int:
    return min(configured_limit or WORKSPACE_PROMPT_HARD_CAP, WORKSPACE_PROMPT_HARD_CAP)
