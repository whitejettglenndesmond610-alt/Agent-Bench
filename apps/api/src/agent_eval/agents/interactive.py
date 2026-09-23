"""HTTP text-protocol agent adapter for tau2-bench interactive evaluation.

This module adapts the platform's dumb HTTP agents (request template with a
single ``{{prompt}}`` placeholder, JSONPath answer extraction) to tau2's
``HalfDuplexAgent`` interface. Every agent turn renders the domain policy, the
tool JSON Schemas, the full transcript and a strict fenced-JSON protocol into
the prompt; the agent's reply is parsed into a respond / tool / stop action.

IMPORTANT: importing this module imports tau2. Only import it lazily inside
worker processes that installed the ``tau2`` dependency group; the API process
must stay importable without tau2.
"""

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from tau2.agent.base_agent import HalfDuplexAgent
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from tau2.environment.tool import Tool

from agent_eval.agents.inference import (
    AgentPromptError,
    AgentPromptResult,
    InferenceConfig,
    post_agent_prompt,
)
from agent_eval.core.tracing import endpoint_host, set_attributes, trace_span

STOP_TOKEN = "###STOP###"
AGENT_PROTOCOL = "http-text-json-v1"
MAX_CORRECTION_REPLY_CHARS = 1000


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


class AgentProtocolError(RuntimeError):
    """The agent repeatedly failed to follow the fenced-JSON protocol."""


class ProtocolParseError(ValueError):
    """A single agent reply could not be parsed into a protocol action."""


@dataclass(frozen=True)
class ProtocolAction:
    action: str
    content: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None


_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_protocol_action(answer: str, *, tool_names: set[str]) -> ProtocolAction:
    match = _FENCE_PATTERN.search(answer)
    if match is None:
        raise ProtocolParseError("reply contains no fenced ```json block")
    try:
        payload: Any = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ProtocolParseError(f"fenced block is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ProtocolParseError("fenced block must contain a JSON object")
    action = payload.get("action")
    if action == "respond":
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProtocolParseError('"respond" action requires a non-empty "content" string')
        return ProtocolAction(action="respond", content=content)
    if action == "tool":
        name = payload.get("name")
        if not isinstance(name, str) or name not in tool_names:
            raise ProtocolParseError(
                f'"tool" action requires a "name" from the available tools {sorted(tool_names)}'
            )
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProtocolParseError('"tool" action "arguments" must be a JSON object')
        return ProtocolAction(action="tool", tool_name=name, tool_arguments=arguments)
    if action == "stop":
        return ProtocolAction(action="stop")
    raise ProtocolParseError('"action" must be one of "respond", "tool" or "stop"')


def render_transcript(history: list[Message]) -> str:
    lines: list[str] = []
    for message in history:
        if isinstance(message, AssistantMessage):
            if message.is_tool_call():
                for call in message.tool_calls or []:
                    lines.append(
                        f"assistant called tool {call.name} with arguments "
                        f"{json.dumps(call.arguments, ensure_ascii=False)}"
                    )
            elif message.content:
                lines.append(f"assistant: {message.content}")
        elif isinstance(message, UserMessage):
            if message.content:
                lines.append(f"user: {message.content}")
        elif isinstance(message, MultiToolMessage):
            for tool_message in message.tool_messages:
                lines.append(_render_tool_message(tool_message))
        elif isinstance(message, ToolMessage):
            lines.append(_render_tool_message(message))
    return "\n".join(lines) if lines else "(no messages yet)"


def _render_tool_message(message: ToolMessage) -> str:
    label = "tool result"
    if isinstance(message.content, str):
        return f"{label}: {message.content}"
    return f"{label}: {json.dumps(message.content, ensure_ascii=False)}"


def render_protocol_prompt(
    *,
    domain_policy: str,
    tools: list[Tool],
    history: list[Message],
    correction: str | None = None,
) -> str:
    tools_json = json.dumps(
        [tool.openai_schema for tool in tools],
        ensure_ascii=False,
        indent=2,
    )
    sections = [
        "You are an agent in a customer-service simulation. Help the user while "
        "strictly following the domain policy.",
        f"# Domain policy\n{domain_policy}",
        f"# Available tools (JSON Schema)\n{tools_json}",
        f"# Conversation so far\n{render_transcript(history)}",
        (
            "# Protocol\n"
            "Reply with exactly one fenced ```json code block and nothing else. "
            'The block must contain a single JSON object with an "action" field:\n'
            '- {"action": "respond", "content": "<what you say to the user>"}\n'
            '- {"action": "tool", "name": "<tool name>", "arguments": {<arguments>}}\n'
            '- {"action": "stop"}\n'
            "Rules:\n"
            '- Use "tool" to call one of the available tools; the tool result is '
            "shown as the next message. Call one tool per reply.\n"
            '- Use "respond" to speak to the user.\n'
            '- Use "stop" only when the user\'s request is fully resolved; '
            "stopping ends the conversation and triggers evaluation.\n"
            "- Do not write anything outside the fenced code block."
        ),
    ]
    if correction is not None:
        sections.append(f"# Correction\n{correction}")
    return "\n\n".join(sections)


class HttpTextProtocolAgent(HalfDuplexAgent):  # type: ignore[misc]
    """Half-duplex tau2 agent backed by one synchronous HTTP call per turn."""

    def __init__(
        self,
        tools: list[Tool],
        domain_policy: str,
        *,
        client: httpx.Client,
        config: InferenceConfig,
    ) -> None:
        super().__init__(tools, domain_policy)
        self._client = client
        self._config = config
        self._tool_names = {tool.name for tool in tools}
        self._correction: str | None = None
        self._spent: list[AgentPromptResult] = []

    @property
    def session_usage(self) -> dict[str, int] | None:
        """Usage of every HTTP call made this episode, including failed parses."""
        return merge_token_usage(self._spent)

    def get_init_state(
        self, message_history: list[Message] | None = None
    ) -> list[Message]:
        return list(message_history or [])

    @classmethod
    def is_stop(cls, message: AssistantMessage) -> bool:
        return (
            not message.is_tool_call()
            and isinstance(message.content, str)
            and STOP_TOKEN in message.content
        )

    def generate_next_message(
        self, message: Message, state: list[Message]
    ) -> tuple[AssistantMessage, list[Message]]:
        history = list(state)
        if message is not None:
            if isinstance(message, MultiToolMessage):
                history.extend(message.tool_messages)
            else:
                history.append(message)
        reply = self._next_reply(history)
        history.append(reply)
        return reply, history

    def _next_reply(self, history: list[Message]) -> AssistantMessage:
        result = self._ask(history)
        results = [result]
        try:
            action = parse_protocol_action(result.answer, tool_names=self._tool_names)
        except ProtocolParseError as first_error:
            if self._correction is not None:
                raise AgentProtocolError(str(first_error)) from first_error
            truncated = result.answer[:MAX_CORRECTION_REPLY_CHARS]
            self._correction = (
                f"Your previous reply was invalid: {first_error}\n"
                f"Your previous reply was:\n{truncated}\n"
                "Reply again with exactly one fenced ```json block that follows "
                "the protocol above."
            )
            retry = self._ask(history)
            results.append(retry)
            try:
                action = parse_protocol_action(retry.answer, tool_names=self._tool_names)
            except ProtocolParseError as second_error:
                raise AgentProtocolError(str(second_error)) from second_error
        self._correction = None
        return self._to_message(action, usage=merge_token_usage(results))

    def _ask(self, history: list[Message]) -> AgentPromptResult:
        prompt = render_protocol_prompt(
            domain_policy=self.domain_policy,
            tools=self.tools,
            history=history,
            correction=self._correction,
        )
        with trace_span(
            "gen_ai.chat agent",
            attributes={
                "gen_ai.system": endpoint_host(self._config.endpoint_url),
                "gen_ai.operation.name": "chat",
                "agent_eval.agent_protocol": AGENT_PROTOCOL,
            },
        ) as span:
            try:
                result = post_agent_prompt(self._client, self._config, prompt)
            except AgentPromptError as error:
                set_attributes(span, {"error.type": error.error_type})
                raise
            self._spent.append(result)
            set_attributes(
                span,
                {
                    "gen_ai.usage.input_tokens": result.input_tokens,
                    "gen_ai.usage.output_tokens": result.output_tokens,
                    "gen_ai.usage.total_tokens": result.total_tokens,
                    "agent_eval.latency_ms": result.latency_ms,
                    "http.status_code": result.status_code,
                },
            )
            return result

    @staticmethod
    def _to_message(
        action: ProtocolAction, *, usage: dict[str, int] | None
    ) -> AssistantMessage:
        if action.action == "respond":
            assert action.content is not None
            return AssistantMessage.text(action.content, cost=0.0, usage=usage)
        if action.action == "tool":
            assert action.tool_name is not None and action.tool_arguments is not None
            return AssistantMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id=uuid.uuid4().hex,
                        name=action.tool_name,
                        arguments=action.tool_arguments,
                        requestor="assistant",
                    )
                ],
                cost=0.0,
                usage=usage,
            )
        return AssistantMessage.text(STOP_TOKEN, cost=0.0, usage=usage)
