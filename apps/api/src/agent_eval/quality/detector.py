from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
from typing import Any
from uuid import UUID

import regex
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.db.base import utc_now
from agent_eval.db.enums import BadPatternFindingStatus, QualityTaskStatus
from agent_eval.db.models import BadPatternFinding, BadPatternRule, QualityReviewTask

MAX_SCAN_CHARACTERS = 20_000
MAX_MATCHES_PER_RULE = 20
SNIPPET_CONTEXT = 90
REGEX_TIMEOUT_SECONDS = 0.05


class InvalidBadPatternRule(ValueError):
    pass


@dataclass(frozen=True)
class PatternMatch:
    count: int
    snippet: str


def compile_pattern(pattern: str) -> Any:
    if not pattern or len(pattern) > 500:
        raise InvalidBadPatternRule("operator pattern must contain 1-500 characters")
    try:
        return regex.compile(pattern, regex.IGNORECASE | regex.MULTILINE)
    except regex.error as error:
        raise InvalidBadPatternRule(f"invalid regular expression: {error}") from error


def match_rule(pattern: str, answer: str) -> PatternMatch | None:
    compiled = compile_pattern(pattern)
    scanned = answer[:MAX_SCAN_CHARACTERS]
    try:
        matches = list(
            islice(
                compiled.finditer(scanned, timeout=REGEX_TIMEOUT_SECONDS),
                MAX_MATCHES_PER_RULE,
            )
        )
    except TimeoutError as error:
        raise InvalidBadPatternRule("regular expression exceeded the scan time limit") from error
    if not matches:
        return None
    first = matches[0]
    start = max(0, first.start() - SNIPPET_CONTEXT)
    end = min(len(scanned), first.end() + SNIPPET_CONTEXT)
    snippet = scanned[start:end].replace("\x00", "").strip()
    return PatternMatch(count=len(matches), snippet=snippet[:500])


async def scan_bad_patterns(
    session: AsyncSession,
    *,
    execution_item_id: UUID,
    answer: str,
    rules: list[BadPatternRule] | None = None,
    refresh_existing: bool = True,
) -> list[BadPatternFinding]:
    if rules is None:
        rules = list(
            await session.scalars(
                select(BadPatternRule)
                .where(BadPatternRule.enabled.is_(True))
                .order_by(BadPatternRule.severity.desc(), BadPatternRule.key)
            )
        )
    existing: dict[UUID, BadPatternFinding] = {}
    if refresh_existing:
        existing = {
            finding.rule_id: finding
            for finding in await session.scalars(
                select(BadPatternFinding).where(
                    BadPatternFinding.execution_item_id == execution_item_id
                )
            )
        }
    detected: list[BadPatternFinding] = []
    detected_rule_ids: set[UUID] = set()
    for rule in rules:
        try:
            matched = match_rule(rule.pattern, answer)
        except InvalidBadPatternRule:
            continue
        finding = existing.get(rule.id)
        if matched is None:
            continue
        detected_rule_ids.add(rule.id)
        if finding is None:
            finding = BadPatternFinding(
                execution_item_id=execution_item_id,
                rule_id=rule.id,
                status=BadPatternFindingStatus.OPEN,
                match_count=matched.count,
                snippet=matched.snippet,
                confidence=Decimal(100),
            )
            session.add(finding)
        elif finding.status == BadPatternFindingStatus.OPEN:
            finding.match_count = matched.count
            finding.snippet = matched.snippet
        detected.append(finding)
    if refresh_existing:
        for rule_id, finding in existing.items():
            if (
                rule_id not in detected_rule_ids
                and finding.status == BadPatternFindingStatus.OPEN
            ):
                finding.status = BadPatternFindingStatus.DISMISSED
                finding.review_note = "重新检测未再命中当前启用规则"
                finding.reviewer_subject_key = "system:rescan"
                finding.reviewer_display_name = "系统重新检测"
                finding.reviewed_at = utc_now()
    if detected:
        task = await session.scalar(
            select(QualityReviewTask).where(
                QualityReviewTask.execution_item_id == execution_item_id
            )
        )
        if task is None:
            session.add(
                QualityReviewTask(
                    execution_item_id=execution_item_id,
                    status=QualityTaskStatus.UNASSIGNED,
                )
            )
    await session.flush()
    return detected
