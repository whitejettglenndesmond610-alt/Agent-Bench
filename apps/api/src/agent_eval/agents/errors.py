import json
from typing import Any


def classify_agent_failure(
    *,
    status_code: int | None,
    error: str | None = None,
    content: Any = None,
) -> str:
    text = f"{error or ''} {_content_text(content)}".casefold()
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 429:
        return "rate_limit"
    if status_code is not None and status_code >= 500:
        return "server_error"
    if status_code == 408 or "timeout" in text:
        return "timeout"
    if any(
        term in text
        for term in (
            "input length",
            "context length",
            "maximum context",
            "prompt is too long",
            "token limit",
            "too many tokens",
        )
    ) or status_code == 413:
        return "input_limit"
    if any(
        term in text
        for term in ("jsonpath", "valid json", "expected string", "answer is empty")
    ):
        return "response_schema"
    if status_code is not None and status_code >= 400:
        return "client_error"
    if "network" in text:
        return "network"
    return "unknown"


def error_type(category: str) -> str:
    return f"agent_{category}"


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
