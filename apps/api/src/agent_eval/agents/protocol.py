"""Shared fenced-JSON text protocol for HTTP agents.

Both the tau2 interactive adapter (``agents/interactive.py``) and the MCP
executor (``worker/mcp_executor.py``) drive the platform's dumb HTTP agents
with the same wire shape: every turn the agent must reply with exactly one
fenced ```json block containing a single action object. This module holds the
tau2-free/mcp-free parsing and correction helpers so neither evaluator
re-implements them.

IMPORTANT: this module must stay importable without the ``tau2`` or ``mcp``
dependency groups.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from agent_eval.agents.inference import AgentPromptResult

MAX_CORRECTION_REPLY_CHARS = 1000

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class AgentProtocolError(RuntimeError):
    """The agent repeatedly failed to follow the fenced-JSON protocol."""


class ProtocolParseError(ValueError):
    """A single agent reply could not be parsed into a protocol action."""


def extract_fenced_payload(answer: str) -> dict[str, Any]:
    """Extract the single JSON object inside the reply's fenced code block."""
    match = _FENCE_PATTERN.search(answer)
    if match is None:
        raise ProtocolParseError("reply contains no fenced ```json block")
    try:
        payload: Any = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ProtocolParseError(f"fenced block is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ProtocolParseError("fenced block must contain a JSON object")
    return payload


def parse_tool_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the "arguments" field of a "tool" action payload."""
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ProtocolParseError('"tool" action "arguments" must be a JSON object')
    return arguments


def build_correction_message(error: ProtocolParseError, reply: str) -> str:
    """One-shot correction prompt appended after an unparseable reply."""
    truncated = reply[:MAX_CORRECTION_REPLY_CHARS]
    return (
        f"Your previous reply was invalid: {error}\n"
        f"Your previous reply was:\n{truncated}\n"
        "Reply again with exactly one fenced ```json block that follows "
        "the protocol above."
    )


def merge_token_usage(results: list[AgentPromptResult]) -> dict[str, int] | None:
    """Sum per-call usage (tau2 dict shape); None when no call reported usage."""
    observed = [
        result
        for result in results
        if result.input_tokens is not None and result.output_tokens is not None
    ]
    if not observed:
        return None
    return {
        "prompt_tokens": sum(result.input_tokens or 0 for result in observed),
        "completion_tokens": sum(result.output_tokens or 0 for result in observed),
    }


# ---------------------------------------------------------------------------
# MCP variant: the agent only calls tools and stops with a final answer.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpAction:
    action: str  # "tool" or "stop"
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    answer: str | None = None


def parse_mcp_action(answer: str, *, tool_names: set[str]) -> McpAction:
    payload = extract_fenced_payload(answer)
    action = payload.get("action")
    if action == "tool":
        name = payload.get("name")
        if not isinstance(name, str) or name not in tool_names:
            raise ProtocolParseError(
                f'"tool" action requires a "name" from the available tools {sorted(tool_names)}'
            )
        return McpAction(
            action="tool", tool_name=name, tool_arguments=parse_tool_arguments(payload)
        )
    if action == "stop":
        final_answer = payload.get("answer")
        if final_answer is not None and not isinstance(final_answer, str):
            raise ProtocolParseError('"stop" action "answer" must be a string when present')
        return McpAction(action="stop", answer=final_answer)
    raise ProtocolParseError('"action" must be one of "tool" or "stop"')


def render_mcp_prompt(
    *,
    task_description: str,
    tool_schemas: list[dict[str, Any]],
    history: list[str],
    workspace: str | None = None,
    correction: str | None = None,
) -> str:
    tools_json = json.dumps(tool_schemas, ensure_ascii=False, indent=2)
    sections = [
        "You are an agent completing a filesystem task through MCP tools. "
        "Use the tools to inspect and modify the workspace, then stop when "
        "the task is done.",
        f"# Task\n{task_description}",
        *(
            [
                f"# Workspace\nAll task files live in the workspace root `{workspace}`. "
                "Prefer absolute paths under this root when calling tools."
            ]
            if workspace is not None
            else []
        ),
        f"# Available tools (JSON Schema)\n{tools_json}",
        f"# Progress so far\n{chr(10).join(history) if history else '(no actions yet)'}",
        (
            "# Protocol\n"
            "Reply with exactly one fenced ```json code block and nothing else. "
            'The block must contain a single JSON object with an "action" field:\n'
            '- {"action": "tool", "name": "<tool name>", "arguments": {<arguments>}}\n'
            '- {"action": "stop", "answer": "<short summary of what you did>"}\n'
            "Rules:\n"
            '- Use "tool" to call one of the available tools; the tool result is '
            "shown as the next message. Call one tool per reply.\n"
            '- Use "stop" only when the task is fully completed; stopping ends '
            "the episode and triggers verification.\n"
            "- Do not write anything outside the fenced code block."
        ),
    ]
    if correction is not None:
        sections.append(f"# Correction\n{correction}")
    return "\n\n".join(sections)
