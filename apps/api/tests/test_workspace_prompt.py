from agent_eval.db.enums import WorkspaceMessageRole
from agent_eval.workspace.prompt import (
    TranscriptMessage,
    format_workspace_comparison_prompt,
    format_workspace_transcript,
)


def test_workspace_prompt_is_direct_chat_continuation() -> None:
    prompt = format_workspace_transcript(
        [
            TranscriptMessage(1, WorkspaceMessageRole.USER, "你现在是什么模型"),
            TranscriptMessage(1, WorkspaceMessageRole.ASSISTANT, "旧回答"),
            TranscriptMessage(2, WorkspaceMessageRole.USER, "用中文回答我"),
            TranscriptMessage(2, WorkspaceMessageRole.ASSISTANT, "未采用的回答"),
            TranscriptMessage(3, WorkspaceMessageRole.USER, "未激活", active=False),
        ],
        current_turn_no=2,
        agent_name="Qwen Math Turbo",
        agent_version=1,
    )

    assert prompt.format_version == "workspace-chat-v2"
    assert 'configured Agent "Qwen Math Turbo" (version 1)' in prompt.content
    assert 'authoritative model identity in this chat is "Qwen Math Turbo"' in prompt.content
    assert "User:\n你现在是什么模型" in prompt.content
    assert "Assistant:\n旧回答" in prompt.content
    assert "User:\n用中文回答我" in prompt.content
    assert "未采用的回答" not in prompt.content
    assert "未激活" not in prompt.content
    assert "canonical JSON" not in prompt.content
    assert "TRANSCRIPT_JSON" not in prompt.content
    assert prompt.content.endswith("Assistant:\n")
    assert prompt.characters == len(prompt.content)


def test_workspace_comparison_prompt_is_agent_neutral_and_exact() -> None:
    prompt = format_workspace_comparison_prompt("同一道题，不增加包装。")

    assert prompt.format_version == "workspace-compare-v1"
    assert prompt.content == "同一道题，不增加包装。"
    assert prompt.characters == len(prompt.content)
