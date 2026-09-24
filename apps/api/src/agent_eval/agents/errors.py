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
    if status_code == 402 or any(
        term in text
        for term in (
            "insufficient balance",
            "balance is insufficient",
            "payment required",
            "billing",
            "recharge",
        )
    ):
        return "billing"
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


def classify_episode_exception(error: Exception, *, prefix: str) -> str:
    """Classify an exception escaping an interactive (tau2/MCP) episode.

    anyio task groups surface failures as ExceptionGroup, so the first leaf is
    unwrapped before classification. LiteLLM-style exceptions carry a
    ``status_code`` attribute (e.g. DeepSeek returns 402 for an exhausted
    balance); known categories map to the ``agent_*`` taxonomy so the result
    validity gate can invalidate affected runs. Unknown errors keep a
    ``{prefix}_{ExceptionName}`` fallback.
    """
    if isinstance(error, ExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    status_code = getattr(error, "status_code", None)
    category = classify_agent_failure(
        status_code=status_code if isinstance(status_code, int) else None,
        error=str(error),
    )
    if category != "unknown":
        return error_type(category)
    return f"{prefix}_{type(error).__name__}"[:80]


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
