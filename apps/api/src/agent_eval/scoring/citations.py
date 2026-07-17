from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

SCORE_QUANTUM = Decimal("0.01")


def calculate_citation_score(
    scores: Sequence[tuple[int | Decimal, int | Decimal]],
    *,
    authenticity_weight: int,
    support_weight: int,
) -> Decimal:
    if not scores:
        return Decimal(0).quantize(SCORE_QUANTUM)
    weight_total = Decimal(authenticity_weight + support_weight)
    total = sum(
        (
            (
                Decimal(authenticity) * authenticity_weight
                + Decimal(support) * support_weight
            )
            / weight_total
            for authenticity, support in scores
        ),
        start=Decimal(0),
    )
    return (total / len(scores)).quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_retrieval_final_score(
    answer_score: int | Decimal,
    citation_score: int | Decimal,
    *,
    answer_weight: int,
    citation_weight: int,
) -> Decimal:
    weight_total = Decimal(answer_weight + citation_weight)
    result = (
        Decimal(answer_score) * answer_weight + Decimal(citation_score) * citation_weight
    ) / weight_total
    return result.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)
