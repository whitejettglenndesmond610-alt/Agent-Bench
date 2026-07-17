import logging

from agent_eval.core.logging import SensitiveDataFilter, redact_log_message


def test_sensitive_values_are_redacted_from_logs() -> None:
    message = (
        "Authorization: Bearer auth-secret Cookie=session-secret; "
        "token=agent-secret password=login-secret api_key=judge-secret "
        "agent_eval_session=session-secret"
    )

    redacted = redact_log_message(message)

    for secret in (
        "auth-secret",
        "session-secret",
        "agent-secret",
        "login-secret",
        "judge-secret",
    ):
        assert secret not in redacted


def test_exception_message_and_traceback_are_not_logged() -> None:
    secret = "raw-prompt-and-response"
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="external_call_failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    assert SensitiveDataFilter().filter(record) is True
    rendered = logging.Formatter("%(message)s").format(record)
    assert secret not in rendered
    assert "RuntimeError" in rendered
