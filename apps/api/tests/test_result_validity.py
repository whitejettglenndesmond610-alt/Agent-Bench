from decimal import Decimal

from agent_eval.db.enums import ResultValidityStatus
from agent_eval.leaderboards.validity import (
    AUTHENTICATION_FAILURE_REASON,
    CONFIGURATION_FAILURE_RATE_REASON,
    NO_EXECUTION_ITEMS_REASON,
    classify_result_validity,
)


def test_configuration_failure_boundary_is_exact() -> None:
    below = classify_result_validity(["agent_client_error"] * 19 + [None] * 81)
    boundary = classify_result_validity(["agent_response_schema"] + [None] * 4)

    assert below.status == ResultValidityStatus.VALID
    assert below.configuration_failure_count == 19
    assert below.configuration_failure_rate == Decimal("19.0000")
    assert below.reason is None
    assert boundary.status == ResultValidityStatus.INVALID
    assert boundary.configuration_failure_rate == Decimal("20.0000")
    assert boundary.reason == CONFIGURATION_FAILURE_RATE_REASON


def test_authentication_immediately_invalidates_result() -> None:
    result = classify_result_validity(["agent_circuit_authentication", *([None] * 99)])

    assert result.status == ResultValidityStatus.INVALID
    assert result.configuration_failure_count == 1
    assert result.configuration_failure_rate == Decimal("1.0000")
    assert result.reason == AUTHENTICATION_FAILURE_REASON


def test_capability_failures_remain_valid_zero_score_evidence() -> None:
    result = classify_result_validity(
        [
            "agent_input_limit",
            "agent_circuit_input_limit",
            "agent_timeout",
            "agent_network_error",
            "agent_server_error",
            None,
        ]
    )

    assert result.status == ResultValidityStatus.VALID
    assert result.configuration_failure_count == 0
    assert result.configuration_failure_rate == Decimal("0.0000")
    assert result.reason is None


def test_empty_result_is_invalid() -> None:
    result = classify_result_validity([])

    assert result.status == ResultValidityStatus.INVALID
    assert result.reason == NO_EXECUTION_ITEMS_REASON
    assert result.configuration_failure_rate == 0
