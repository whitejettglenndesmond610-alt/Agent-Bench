import logging
import re
import sys

SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(authorization|cookie|token|password|api[_ -]?key)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)\bagent_eval_(session|csrf)=[^\s,;]+"),
)


def redact_log_message(message: str) -> str:
    redacted = message
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}=[REDACTED]"
                if match.lastindex
                else "Bearer [REDACTED]"
            ),
            redacted,
        )
    return redacted


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = redact_log_message(record.getMessage())
        if record.exc_info is not None:
            exception = record.exc_info[1]
            message = f"{message} exception_type={type(exception).__name__}"
            record.exc_info = None
            record.exc_text = None
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(SensitiveDataFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
