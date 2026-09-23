import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx

from agent_eval.agents.errors import classify_agent_failure, error_type
from agent_eval.agents.http_headers import json_request_headers
from agent_eval.agents.mapping import (
    ResponseMappingError,
    extract_answer,
    extract_citations,
    render_request_template,
)
from agent_eval.agents.redaction import redact_content, redact_secret
from agent_eval.db.enums import AttemptStatus, AuthType

MAX_RESPONSE_BYTES = 1_048_576


class AgentPromptError(RuntimeError):
    """Single-shot agent HTTP call failure with a classified error type."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


@dataclass(frozen=True)
class AgentPromptResult:
    answer: str
    content: bytes
    status_code: int
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class InferenceConfig:
    endpoint_url: str
    auth_type: AuthType
    token: str | None
    request_template: str
    answer_jsonpath: str
    citations_jsonpath: str | None
    timeout_seconds: int
    extra_headers: dict[str, str] = field(default_factory=dict)
    api_key_header: str | None = None
    # OpenAI-compatible SSE streaming (chat.completion.chunk deltas).
    supports_streaming: bool = False


@dataclass(frozen=True)
class AttemptCompletion:
    attempt_id: UUID
    status: AttemptStatus
    status_code: int | None
    latency_ms: int
    error_type: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class InferenceOutcome:
    success: bool
    canceled: bool
    answer: str | None
    citations: list[str]
    response_content: bytes | None
    latency_ms: int
    attempt_count: int
    error_type: str | None


StartAttempt = Callable[[int], Awaitable[UUID | None]]
FinishAttempt = Callable[[AttemptCompletion], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
PartialCallback = Callable[[str], Awaitable[None]]


class StreamChunkError(RuntimeError):
    """An SSE chunk carrying an error payload instead of a content delta."""


def _stream_error_text(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return str(error)


def sse_delta_content(payload: Any) -> str | None:
    """Extract one OpenAI-compatible chat.completion.chunk content delta."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return content if isinstance(content, str) else None


def post_agent_prompt(
    client: httpx.Client,
    config: InferenceConfig,
    prompt: str,
) -> AgentPromptResult:
    """Synchronously POST one rendered prompt and extract the mapped answer.

    Shared by the interactive (tau2) adapter: template rendering, auth headers,
    redaction, and JSONPath answer extraction behave exactly like execute_inference,
    but retries and attempt bookkeeping are left to the caller.
    """
    request_body = render_request_template(config.request_template, prompt)
    headers = json_request_headers(
        auth_type=config.auth_type,
        token=config.token,
        extra_headers=config.extra_headers,
        api_key_header=config.api_key_header,
    )
    started_at = perf_counter()
    try:
        response = client.post(
            config.endpoint_url,
            headers=headers,
            json=request_body,
            timeout=httpx.Timeout(config.timeout_seconds),
        )
        content = response.read()
    except httpx.TimeoutException as error:
        raise AgentPromptError(error_type("timeout")) from error
    except (httpx.RequestError, UnicodeError, ValueError) as error:
        raise AgentPromptError(error_type("network")) from error
    latency_ms = elapsed_ms(started_at)
    if len(content) > MAX_RESPONSE_BYTES:
        raise AgentPromptError("agent_response_too_large")
    redacted = redact_content(bytes(content), config.token)
    if response.status_code == 429:
        raise AgentPromptError(error_type("rate_limit"))
    if response.status_code == 408:
        raise AgentPromptError(error_type("timeout"))
    if 500 <= response.status_code <= 599:
        raise AgentPromptError(error_type("server_error"))
    if not response.is_success:
        raise AgentPromptError(
            error_type(
                classify_agent_failure(status_code=response.status_code, content=redacted)
            )
        )
    try:
        payload: Any = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AgentPromptError(error_type("response_schema")) from error
    try:
        answer = extract_answer(payload, config.answer_jsonpath)
    except ResponseMappingError as error:
        raise AgentPromptError(
            error_type(classify_agent_failure(status_code=response.status_code, error=str(error)))
        ) from error
    input_tokens, output_tokens, total_tokens = extract_token_usage(payload)
    return AgentPromptResult(
        answer=redact_secret(answer, config.token),
        content=redacted,
        status_code=response.status_code,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


async def execute_inference(
    client: httpx.AsyncClient,
    config: InferenceConfig,
    prompt: str,
    *,
    previous_attempt_count: int,
    start_attempt: StartAttempt,
    finish_attempt: FinishAttempt,
    sleep: Sleep = asyncio.sleep,
) -> InferenceOutcome:
    request_body = render_request_template(config.request_template, prompt)
    headers = json_request_headers(
        auth_type=config.auth_type,
        token=config.token,
        extra_headers=config.extra_headers,
        api_key_header=config.api_key_header,
    )

    total_latency = 0
    last_content: bytes | None = None
    last_error = "agent_attempts_exhausted"
    attempts_made = previous_attempt_count
    for attempt_no in range(previous_attempt_count + 1, 3):
        attempt_id = await start_attempt(attempt_no)
        if attempt_id is None:
            return InferenceOutcome(
                success=False,
                canceled=True,
                answer=None,
                citations=[],
                response_content=None,
                latency_ms=total_latency,
                attempt_count=attempts_made,
                error_type="run_canceled",
            )
        attempts_made = attempt_no
        started_at = perf_counter()
        try:
            async with client.stream(
                "POST",
                config.endpoint_url,
                headers=headers,
                json=request_body,
                timeout=httpx.Timeout(config.timeout_seconds),
            ) as response:
                content = bytearray()
                response_too_large = False
                async for chunk in response.aiter_bytes():
                    remaining = MAX_RESPONSE_BYTES + 1 - len(content)
                    content.extend(chunk[:remaining])
                    if len(chunk) > remaining or len(content) > MAX_RESPONSE_BYTES:
                        response_too_large = True
                        break
        except httpx.TimeoutException:
            latency_ms = elapsed_ms(started_at)
            total_latency += latency_ms
            last_error = error_type("timeout")
            await finish_attempt(
                AttemptCompletion(
                    attempt_id=attempt_id,
                    status=AttemptStatus.FAILED,
                    status_code=None,
                    latency_ms=latency_ms,
                    error_type=last_error,
                )
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break
        except (httpx.RequestError, UnicodeError, ValueError):
            latency_ms = elapsed_ms(started_at)
            total_latency += latency_ms
            last_error = error_type("network")
            await finish_attempt(
                AttemptCompletion(
                    attempt_id=attempt_id,
                    status=AttemptStatus.FAILED,
                    status_code=None,
                    latency_ms=latency_ms,
                    error_type=last_error,
                )
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break

        latency_ms = elapsed_ms(started_at)
        total_latency += latency_ms
        if response_too_large:
            last_content = None
            last_error = "agent_response_too_large"
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            break
        last_content = redact_content(bytes(content), config.token)
        if response.status_code == 429:
            last_error = error_type("rate_limit")
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            if attempt_no == 1:
                await sleep(retry_delay(response.headers.get("Retry-After")))
                continue
            break
        if 500 <= response.status_code <= 599:
            last_error = error_type("server_error")
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break
        if response.status_code == 408:
            last_error = error_type("timeout")
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break
        if not response.is_success:
            last_error = error_type(
                classify_agent_failure(status_code=response.status_code, content=last_content)
            )
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            break

        try:
            payload: Any = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            last_error = error_type("response_schema")
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            break
        try:
            answer = extract_answer(payload, config.answer_jsonpath)
            citations = extract_citations(payload, config.citations_jsonpath, answer=answer)
        except ResponseMappingError as error:
            last_error = error_type(
                classify_agent_failure(status_code=response.status_code, error=str(error))
            )
            await finish_attempt(
                failed_attempt(attempt_id, response.status_code, latency_ms, last_error)
            )
            break

        input_tokens, output_tokens, total_tokens = extract_token_usage(payload)
        await finish_attempt(
            AttemptCompletion(
                attempt_id=attempt_id,
                status=AttemptStatus.SUCCEEDED,
                status_code=response.status_code,
                latency_ms=latency_ms,
                error_type=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        )
        return InferenceOutcome(
            success=True,
            canceled=False,
            answer=redact_secret(answer, config.token),
            citations=redact_secret(citations, config.token),
            response_content=last_content,
            latency_ms=total_latency,
            attempt_count=attempts_made,
            error_type=None,
        )

    return InferenceOutcome(
        success=False,
        canceled=False,
        answer=None,
        citations=[],
        response_content=last_content,
        latency_ms=total_latency,
        attempt_count=attempts_made,
        error_type=last_error,
    )


async def execute_streaming_inference(
    client: httpx.AsyncClient,
    config: InferenceConfig,
    prompt: str,
    *,
    previous_attempt_count: int,
    start_attempt: StartAttempt,
    finish_attempt: FinishAttempt,
    on_partial: PartialCallback,
    sleep: Sleep = asyncio.sleep,
) -> InferenceOutcome:
    """OpenAI-compatible SSE variant of execute_inference for workspace attempts.

    Sends the rendered template with "stream": true and accumulates
    chat.completion.chunk deltas, reporting the accumulated text through
    on_partial. Token usage is only recorded when the provider includes it in
    the stream (no stream_options is sent, for maximum preset compatibility).
    """
    request_body = render_request_template(config.request_template, prompt)
    request_body["stream"] = True
    headers = json_request_headers(
        auth_type=config.auth_type,
        token=config.token,
        extra_headers=config.extra_headers,
        api_key_header=config.api_key_header,
    )

    total_latency = 0
    last_content: bytes | None = None
    last_error = "agent_attempts_exhausted"
    attempts_made = previous_attempt_count
    for attempt_no in range(previous_attempt_count + 1, 3):
        attempt_id = await start_attempt(attempt_no)
        if attempt_id is None:
            return InferenceOutcome(
                success=False,
                canceled=True,
                answer=None,
                citations=[],
                response_content=None,
                latency_ms=total_latency,
                attempt_count=attempts_made,
                error_type="run_canceled",
            )
        attempts_made = attempt_no
        started_at = perf_counter()
        answer_parts: list[str] = []
        usage_payload: Any = None
        raw = bytearray()
        status_code: int | None = None
        response_too_large = False
        try:
            async with client.stream(
                "POST",
                config.endpoint_url,
                headers=headers,
                json=request_body,
                timeout=httpx.Timeout(config.timeout_seconds),
            ) as response:
                status_code = response.status_code
                if response.is_success:
                    async for line in response.aiter_lines():
                        raw.extend(line.encode("utf-8", errors="replace"))
                        raw.extend(b"\n")
                        if len(raw) > MAX_RESPONSE_BYTES:
                            response_too_large = True
                            break
                        if not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk: Any = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(chunk, dict) and chunk.get("error") is not None:
                            raise StreamChunkError(_stream_error_text(chunk["error"]))
                        delta = sse_delta_content(chunk)
                        if delta:
                            answer_parts.append(delta)
                            await on_partial("".join(answer_parts))
                        if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                            usage_payload = chunk["usage"]
                else:
                    async for chunk in response.aiter_bytes():
                        remaining = MAX_RESPONSE_BYTES + 1 - len(raw)
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining or len(raw) > MAX_RESPONSE_BYTES:
                            response_too_large = True
                            break
        except httpx.TimeoutException:
            latency_ms = elapsed_ms(started_at)
            total_latency += latency_ms
            last_error = error_type("timeout")
            await finish_attempt(
                AttemptCompletion(
                    attempt_id=attempt_id,
                    status=AttemptStatus.FAILED,
                    status_code=None,
                    latency_ms=latency_ms,
                    error_type=last_error,
                )
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break
        except StreamChunkError as error:
            latency_ms = elapsed_ms(started_at)
            total_latency += latency_ms
            # Mid-stream error payloads arrive with HTTP 200; when the message
            # does not classify to a concrete category, treat it as a
            # provider-side server error.
            category = classify_agent_failure(status_code=None, error=str(error))
            last_error = error_type("server_error" if category == "unknown" else category)
            await finish_attempt(
                AttemptCompletion(
                    attempt_id=attempt_id,
                    status=AttemptStatus.FAILED,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    error_type=last_error,
                )
            )
            break
        except (httpx.RequestError, UnicodeError, ValueError):
            latency_ms = elapsed_ms(started_at)
            total_latency += latency_ms
            last_error = error_type("network")
            await finish_attempt(
                AttemptCompletion(
                    attempt_id=attempt_id,
                    status=AttemptStatus.FAILED,
                    status_code=None,
                    latency_ms=latency_ms,
                    error_type=last_error,
                )
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break

        latency_ms = elapsed_ms(started_at)
        total_latency += latency_ms
        if status_code is None:
            # Unreachable in practice: a completed client.stream always yields
            # a status code; guard so the comparisons below stay typed.
            last_error = error_type("network")
            break
        if response_too_large:
            last_content = None
            last_error = "agent_response_too_large"
            await finish_attempt(
                failed_attempt(attempt_id, status_code, latency_ms, last_error)
            )
            break
        last_content = redact_content(bytes(raw), config.token)
        if status_code == 429:
            last_error = error_type("rate_limit")
            await finish_attempt(
                failed_attempt(attempt_id, status_code, latency_ms, last_error)
            )
            if attempt_no == 1:
                await sleep(retry_delay(response.headers.get("Retry-After")))
                continue
            break
        if 500 <= status_code <= 599:
            last_error = error_type("server_error")
            await finish_attempt(
                failed_attempt(attempt_id, status_code, latency_ms, last_error)
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break
        if status_code == 408:
            last_error = error_type("timeout")
            await finish_attempt(
                failed_attempt(attempt_id, status_code, latency_ms, last_error)
            )
            if attempt_no == 1:
                await sleep(1)
                continue
            break
        if not 200 <= status_code <= 299:
            last_error = error_type(
                classify_agent_failure(status_code=status_code, content=last_content)
            )
            await finish_attempt(
                failed_attempt(attempt_id, status_code, latency_ms, last_error)
            )
            break

        answer = "".join(answer_parts).strip()
        if not answer:
            last_error = error_type(
                classify_agent_failure(status_code=status_code, error="answer is empty")
            )
            await finish_attempt(
                failed_attempt(attempt_id, status_code, latency_ms, last_error)
            )
            break

        input_tokens, output_tokens, total_tokens = extract_token_usage(
            {"usage": usage_payload} if usage_payload is not None else None
        )
        await finish_attempt(
            AttemptCompletion(
                attempt_id=attempt_id,
                status=AttemptStatus.SUCCEEDED,
                status_code=status_code,
                latency_ms=latency_ms,
                error_type=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        )
        return InferenceOutcome(
            success=True,
            canceled=False,
            answer=redact_secret(answer, config.token),
            citations=[],
            response_content=last_content,
            latency_ms=total_latency,
            attempt_count=attempts_made,
            error_type=None,
        )

    return InferenceOutcome(
        success=False,
        canceled=False,
        answer=None,
        citations=[],
        response_content=last_content,
        latency_ms=total_latency,
        attempt_count=attempts_made,
        error_type=last_error,
    )


def elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def extract_token_usage(payload: Any) -> tuple[int | None, int | None, int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return None, None, None
    usage = payload["usage"]
    input_tokens = usage_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = usage_value(usage, "completion_tokens", "output_tokens")
    total_tokens = usage_value(usage, "total_tokens")
    if input_tokens is not None and output_tokens is not None:
        computed_total = input_tokens + output_tokens
        if "total_tokens" not in usage:
            total_tokens = computed_total
        elif total_tokens is not None and total_tokens != computed_total:
            total_tokens = None
    return input_tokens, output_tokens, total_tokens


def usage_value(usage: dict[object, object], *keys: str) -> int | None:
    values = {
        value
        for key in keys
        if key in usage
        and isinstance((value := usage[key]), int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return values.pop() if len(values) == 1 else None


def failed_attempt(
    attempt_id: UUID, status_code: int | None, latency_ms: int, error_type: str
) -> AttemptCompletion:
    return AttemptCompletion(
        attempt_id=attempt_id,
        status=AttemptStatus.FAILED,
        status_code=status_code,
        latency_ms=latency_ms,
        error_type=error_type,
    )


def retry_delay(value: str | None) -> float:
    if value is None:
        return 1
    try:
        return min(60, max(0, float(value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return min(60, max(0, (retry_at - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 1
