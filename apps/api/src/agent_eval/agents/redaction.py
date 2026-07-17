import json
from typing import Any


def redact_secret(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            redact_secret(key, secret): redact_secret(item, secret) for key, item in value.items()
        }
    return value


def redact_content(content: bytes, secret: str | None) -> bytes:
    if not secret:
        return content
    try:
        payload: Any = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1].encode()
        return content.replace(secret.encode(), b"[REDACTED]").replace(escaped, b"[REDACTED]")
    return json.dumps(
        redact_secret(payload, secret),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
