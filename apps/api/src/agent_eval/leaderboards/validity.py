from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from agent_eval.db.enums import ResultValidityStatus

AUTHENTICATION_FAILURES = frozenset(
    {"agent_authentication", "agent_circuit_authentication", "agent_billing"}
)
CONFIGURATION_FAILURES = AUTHENTICATION_FAILURES | frozenset(
    {
        "agent_response_schema",
        "agent_circuit_response_schema",
        "agent_client_error",
        "agent_circuit_client_error",
    }
)
NO_EXECUTION_ITEMS_REASON = "no_execution_items"
AUTHENTICATION_FAILURE_REASON = "authentication_failure"
CONFIGURATION_FAILURE_RATE_REASON = "configuration_failure_rate_at_least_20_percent"
PERCENT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class ResultValidity:
    status: ResultValidityStatus
    reason: str | None
    configuration_failure_count: int
    configuration_failure_rate: Decimal


def classify_result_validity(error_types: Iterable[str | None]) -> ResultValidity:
    errors = tuple(error_types)
    total = len(errors)
    configuration_failure_count = sum(error in CONFIGURATION_FAILURES for error in errors)
    authentication_failure_count = sum(error in AUTHENTICATION_FAILURES for error in errors)
    rate = (
        (Decimal(configuration_failure_count) * 100 / Decimal(total)).quantize(
            PERCENT_QUANTUM, rounding=ROUND_HALF_UP
        )
        if total
        else Decimal(0)
    )

    reason: str | None = None
    if total == 0:
        reason = NO_EXECUTION_ITEMS_REASON
    elif authentication_failure_count:
        reason = AUTHENTICATION_FAILURE_REASON
    elif configuration_failure_count * 5 >= total:
        reason = CONFIGURATION_FAILURE_RATE_REASON

    return ResultValidity(
        status=(ResultValidityStatus.INVALID if reason else ResultValidityStatus.VALID),
        reason=reason,
        configuration_failure_count=configuration_failure_count,
        configuration_failure_rate=rate,
    )
