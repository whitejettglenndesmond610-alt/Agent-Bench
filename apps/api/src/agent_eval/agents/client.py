import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import httpx

from agent_eval.agents.http_headers import json_request_headers
from agent_eval.agents.mapping import (
    ResponseMappingError,
    extract_answer,
    extract_citations,
    render_request_template,
)
from agent_eval.agents.redaction import redact_secret
from agent_eval.db.enums import AuthType


@dataclass(frozen=True)
class ConnectionConfig:
    endpoint_url: str
    auth_type: AuthType
    token: str | None
    request_template: str
    answer_jsonpath: str
    citations_jsonpath: str | None
    timeout_seconds: int
    extra_headers: dict[str, str] = field(default_factory=dict)
    api_key_header: str | None = None


@dataclass(frozen=True)
class ConnectionTestResult:
    success: bool
    status_code: int | None
    latency_ms: int
    request_body: dict[str, Any]
    raw_response: Any | None
    answer: str | None
    citations: list[str]
    error: str | None


async def test_connection(
    config: ConnectionConfig,
    prompt: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_response_bytes: int = 1_048_576,
) -> ConnectionTestResult:
    request_body = render_request_template(config.request_template, prompt)
    headers = json_request_headers(
        auth_type=config.auth_type,
        token=config.token,
        extra_headers=config.extra_headers,
        api_key_header=config.api_key_header,
    )

    started_at = perf_counter()
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(config.timeout_seconds),
                follow_redirects=False,
            ) as client,
            client.stream(
                "POST", config.endpoint_url, headers=headers, json=request_body
            ) as response,
        ):
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_response_bytes:
                    return _failed_result(
                        started_at,
                        request_body,
                        response.status_code,
                        "response exceeds 1 MB limit",
                    )
    except httpx.TimeoutException:
        return _failed_result(started_at, request_body, None, "agent_timeout")
    except (httpx.RequestError, UnicodeError, ValueError):
        return _failed_result(started_at, request_body, None, "agent_network_error")

    latency_ms = elapsed_ms(started_at)
    try:
        raw_response = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response_text = content.decode("utf-8", errors="replace")
        return ConnectionTestResult(
            success=False,
            status_code=response.status_code,
            latency_ms=latency_ms,
            request_body=request_body,
            raw_response=redact_secret(response_text, config.token),
            answer=None,
            citations=[],
            error="response is not valid JSON",
        )

    if not response.is_success:
        return ConnectionTestResult(
            success=False,
            status_code=response.status_code,
            latency_ms=latency_ms,
            request_body=request_body,
            raw_response=redact_secret(raw_response, config.token),
            answer=None,
            citations=[],
            error=f"Agent returned HTTP {response.status_code}",
        )

    try:
        answer = extract_answer(raw_response, config.answer_jsonpath)
        citations = extract_citations(raw_response, config.citations_jsonpath, answer=answer)
    except ResponseMappingError as error:
        return ConnectionTestResult(
            success=False,
            status_code=response.status_code,
            latency_ms=latency_ms,
            request_body=request_body,
            raw_response=redact_secret(raw_response, config.token),
            answer=None,
            citations=[],
            error=str(error),
        )

    return ConnectionTestResult(
        success=True,
        status_code=response.status_code,
        latency_ms=latency_ms,
        request_body=request_body,
        raw_response=redact_secret(raw_response, config.token),
        answer=redact_secret(answer, config.token),
        citations=redact_secret(citations, config.token),
        error=None,
    )


def elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def _failed_result(
    started_at: float,
    request_body: dict[str, Any],
    status_code: int | None,
    error: str,
) -> ConnectionTestResult:
    return ConnectionTestResult(
        success=False,
        status_code=status_code,
        latency_ms=elapsed_ms(started_at),
        request_body=request_body,
        raw_response=None,
        answer=None,
        citations=[],
        error=error,
    )
