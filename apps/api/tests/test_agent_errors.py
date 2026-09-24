from agent_eval.agents.errors import classify_agent_failure, classify_episode_exception


def test_classify_agent_failure_detects_billing_by_status() -> None:
    assert classify_agent_failure(status_code=402) == "billing"


def test_classify_agent_failure_detects_billing_by_message() -> None:
    assert (
        classify_agent_failure(status_code=400, error="Insufficient Balance")
        == "billing"
    )


def test_classify_agent_failure_billing_beats_generic_client_error() -> None:
    assert (
        classify_agent_failure(status_code=400, error="payment required for this key")
        == "billing"
    )


def test_classify_episode_exception_unwraps_exception_group() -> None:
    group = ExceptionGroup("episode", [RuntimeError("plain failure")])
    assert classify_episode_exception(group, prefix="interactive_episode") == (
        "interactive_episode_RuntimeError"
    )


def test_classify_episode_exception_maps_known_categories() -> None:
    class FakeLiteLLMError(Exception):
        status_code = 402

    assert classify_episode_exception(
        FakeLiteLLMError("Insufficient Balance"), prefix="mcp_episode"
    ) == "agent_billing"


def test_classify_episode_exception_unknown_keeps_prefix() -> None:
    assert classify_episode_exception(
        ValueError("weird"), prefix="mcp_episode"
    ) == "mcp_episode_ValueError"
