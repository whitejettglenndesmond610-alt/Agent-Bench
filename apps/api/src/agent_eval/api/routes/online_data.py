import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from agent_eval.api.dependencies import DatabaseSession, OperatorCsrfSession, OperatorSession
from agent_eval.api.mutation_guard import sqlite_mutation_guard
from agent_eval.api.routes.workspace import request_fingerprint
from agent_eval.api.schemas.online_data import (
    OnlineDataBatchCreate,
    OnlineDataBatchList,
    OnlineDataBatchOperationResponse,
    OnlineDataBatchResponse,
    OnlineDataBatchReviewInput,
    OnlineDataRecordDetail,
    OnlineDataRecordListItem,
    OnlineDataRecordPage,
    OnlineDataSummary,
    OnlineMatchedRule,
    OnlineTraceEvent,
)
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    BadPatternSeverity,
    OnlineDataEnvironment,
    OnlineDataRecordStatus,
)
from agent_eval.db.models import (
    BadPatternRule,
    OnlineDataBatch,
    OnlineDataRecord,
)
from agent_eval.quality.detector import InvalidBadPatternRule, match_rule

router = APIRouter(prefix="/api/v1/online-data", tags=["online-data"])

SEVERITY_RANK = {
    BadPatternSeverity.LOW: 1,
    BadPatternSeverity.MEDIUM: 2,
    BadPatternSeverity.HIGH: 3,
    BadPatternSeverity.CRITICAL: 4,
}


def online_data_error(http_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def batch_response(batch: OnlineDataBatch) -> OnlineDataBatchResponse:
    return OnlineDataBatchResponse(
        id=batch.id,
        source_name=batch.source_name,
        environment=batch.environment,
        external_batch_id=batch.external_batch_id,
        received_count=batch.received_count,
        accepted_count=batch.accepted_count,
        duplicate_count=batch.duplicate_count,
        duplicate_errors=list(batch.duplicate_errors_json),
        creator_display_name=batch.creator_display_name,
        created_at=batch.created_at,
    )


def record_response(
    record: OnlineDataRecord, batch: OnlineDataBatch
) -> OnlineDataRecordListItem:
    return OnlineDataRecordListItem(
        id=record.id,
        batch_id=batch.id,
        source_name=batch.source_name,
        environment=batch.environment,
        external_id=record.external_id,
        agent_name=record.agent_name,
        agent_version=record.agent_version,
        prompt_preview=record.prompt[:300],
        answer_preview=record.answer[:500],
        occurred_at=record.occurred_at,
        status=record.status,
        risk_severity=record.risk_severity,
        match_count=record.match_count,
        matched_rules=[
            OnlineMatchedRule.model_validate(rule) for rule in record.matched_rules_json
        ],
        reviewer_display_name=record.reviewer_display_name,
        review_note=record.review_note,
        reviewed_at=record.reviewed_at,
    )


def scan_online_answer(
    answer: str, rules: list[BadPatternRule]
) -> tuple[list[dict[str, Any]], BadPatternSeverity | None, int]:
    matches: list[dict[str, Any]] = []
    for rule in rules:
        try:
            matched = match_rule(rule.pattern, answer)
        except InvalidBadPatternRule:
            continue
        if matched is None:
            continue
        matches.append(
            {
                "key": rule.key,
                "name": rule.name,
                "category": rule.category,
                "severity": rule.severity.value,
                "match_count": matched.count,
                "snippet": matched.snippet,
            }
        )
    risk = max(
        (BadPatternSeverity(match["severity"]) for match in matches),
        key=SEVERITY_RANK.__getitem__,
        default=None,
    )
    return matches, risk, sum(int(match["match_count"]) for match in matches)


@router.post(
    "/batches",
    response_model=OnlineDataBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_online_data_batch(
    payload: OnlineDataBatchCreate,
    request: Request,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> OnlineDataBatchResponse:
    user = authorization.user
    if user is None:
        raise online_data_error(401, "user_missing", "登录已失效")
    creator_user_id = None if user.is_shared else user.id
    creator_subject_key = user.subject_key
    creator_display_name = user.display_name
    fingerprint = request_fingerprint(payload.model_dump(mode="json"))
    existing = await database.scalar(
        select(OnlineDataBatch).where(
            OnlineDataBatch.idempotency_key == str(payload.idempotency_key)
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise online_data_error(409, "idempotency_conflict", "幂等键已用于其他回流批次")
        return batch_response(existing)
    async with sqlite_mutation_guard(
        request, database, f"online-data-batch:{payload.source_name}"
    ):
        return await create_online_data_batch_locked(
            payload,
            database,
            creator_user_id,
            creator_subject_key,
            creator_display_name,
            fingerprint,
        )


async def create_online_data_batch_locked(
    payload: OnlineDataBatchCreate,
    database: DatabaseSession,
    creator_user_id: UUID | None,
    creator_subject_key: str,
    creator_display_name: str,
    fingerprint: str,
) -> OnlineDataBatchResponse:
    source_batch = await database.scalar(
        select(OnlineDataBatch).where(
            OnlineDataBatch.source_name == payload.source_name,
            OnlineDataBatch.external_batch_id == payload.external_batch_id,
        )
    )
    if source_batch is not None:
        if (
            source_batch.idempotency_key == str(payload.idempotency_key)
            and source_batch.request_fingerprint == fingerprint
        ):
            return batch_response(source_batch)
        raise online_data_error(409, "external_batch_exists", "来源批次编号已导入")
    dedupe_keys = {
        record.external_id: hashlib.sha256(
            f"{payload.source_name}\0{record.external_id}".encode()
        ).hexdigest()
        for record in payload.records
    }
    existing_dedupe = set(
        await database.scalars(
            select(OnlineDataRecord.dedupe_key).where(
                OnlineDataRecord.dedupe_key.in_(dedupe_keys.values())
            )
        )
    )
    duplicate_errors = [
        {"external_id": external_id, "code": "source_record_exists"}
        for external_id, dedupe_key in dedupe_keys.items()
        if dedupe_key in existing_dedupe
    ]
    rules = list(
        await database.scalars(
            select(BadPatternRule)
            .where(BadPatternRule.enabled.is_(True))
            .order_by(BadPatternRule.key)
        )
    )
    batch = OnlineDataBatch(
        source_name=payload.source_name,
        environment=payload.environment,
        external_batch_id=payload.external_batch_id,
        idempotency_key=str(payload.idempotency_key),
        request_fingerprint=fingerprint,
        received_count=len(payload.records),
        accepted_count=len(payload.records) - len(duplicate_errors),
        duplicate_count=len(duplicate_errors),
        duplicate_errors_json=duplicate_errors,
        created_by_user_id=creator_user_id,
        creator_subject_key=creator_subject_key,
        creator_display_name=creator_display_name,
    )
    database.add(batch)
    await database.flush()
    for record in payload.records:
        dedupe_key = dedupe_keys[record.external_id]
        if dedupe_key in existing_dedupe:
            continue
        record_payload = record.model_dump(mode="json")
        matches, risk, match_count = scan_online_answer(record.answer, rules)
        database.add(
            OnlineDataRecord(
                batch_id=batch.id,
                external_id=record.external_id,
                dedupe_key=dedupe_key,
                content_hash=canonical_hash(record_payload),
                agent_name=record.agent_name,
                agent_version=record.agent_version,
                prompt=record.prompt,
                answer=record.answer,
                trace_json=[event.model_dump(mode="json") for event in record.trace],
                metadata_json=record.metadata,
                occurred_at=record.occurred_at,
                status=OnlineDataRecordStatus.PENDING,
                risk_severity=risk,
                match_count=match_count,
                matched_rules_json=matches,
            )
        )
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        duplicate = await database.scalar(
            select(OnlineDataBatch).where(
                OnlineDataBatch.idempotency_key == str(payload.idempotency_key)
            )
        )
        if duplicate is not None and duplicate.request_fingerprint == fingerprint:
            return batch_response(duplicate)
        raise online_data_error(409, "online_batch_conflict", "回流批次发生并发冲突") from error
    await database.refresh(batch)
    return batch_response(batch)


@router.get("/summary", response_model=OnlineDataSummary)
async def get_online_data_summary(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> OnlineDataSummary:
    status_rows = (
        await database.execute(
            select(OnlineDataRecord.status, func.count()).group_by(OnlineDataRecord.status)
        )
    ).all()
    counts = {record_status: count for record_status, count in status_rows}
    return OnlineDataSummary(
        total_batches=(
            await database.scalar(select(func.count()).select_from(OnlineDataBatch))
        )
        or 0,
        total_records=(
            await database.scalar(select(func.count()).select_from(OnlineDataRecord))
        )
        or 0,
        pending_records=counts.get(OnlineDataRecordStatus.PENDING, 0),
        retained_records=counts.get(OnlineDataRecordStatus.RETAINED, 0),
        discarded_records=counts.get(OnlineDataRecordStatus.DISCARDED, 0),
        risky_records=(
            await database.scalar(
                select(func.count())
                .select_from(OnlineDataRecord)
                .where(OnlineDataRecord.risk_severity.is_not(None))
            )
        )
        or 0,
        latest_received_at=await database.scalar(select(func.max(OnlineDataBatch.created_at))),
    )


@router.get("/batches", response_model=OnlineDataBatchList)
async def list_online_data_batches(
    database: DatabaseSession,
    _authorization: OperatorSession,
    limit: int = Query(default=20, ge=1, le=100),
) -> OnlineDataBatchList:
    batches = list(
        await database.scalars(
            select(OnlineDataBatch)
            .order_by(OnlineDataBatch.created_at.desc(), OnlineDataBatch.id.desc())
            .limit(limit)
        )
    )
    return OnlineDataBatchList(items=[batch_response(batch) for batch in batches])


@router.get("/records", response_model=OnlineDataRecordPage)
async def list_online_data_records(
    database: DatabaseSession,
    _authorization: OperatorSession,
    record_status: Annotated[OnlineDataRecordStatus | None, Query(alias="status")] = None,
    environment: OnlineDataEnvironment | None = None,
    risk_severity: BadPatternSeverity | None = None,
    source_name: Annotated[str | None, Query(max_length=80)] = None,
    agent_name: Annotated[str | None, Query(max_length=160)] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> OnlineDataRecordPage:
    filters = []
    if record_status is not None:
        filters.append(OnlineDataRecord.status == record_status)
    if environment is not None:
        filters.append(OnlineDataBatch.environment == environment)
    if risk_severity is not None:
        filters.append(OnlineDataRecord.risk_severity == risk_severity)
    if source_name:
        filters.append(OnlineDataBatch.source_name == source_name.strip())
    if agent_name:
        filters.append(OnlineDataRecord.agent_name == agent_name.strip())
    if search:
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                OnlineDataRecord.external_id.ilike(term),
                OnlineDataRecord.agent_name.ilike(term),
            )
        )
    if cursor is not None:
        cursor_record = await database.get(OnlineDataRecord, cursor)
        if cursor_record is None:
            raise online_data_error(422, "invalid_cursor", "分页游标无效或已过期")
        filters.append(
            or_(
                OnlineDataRecord.occurred_at < cursor_record.occurred_at,
                and_(
                    OnlineDataRecord.occurred_at == cursor_record.occurred_at,
                    OnlineDataRecord.id < cursor_record.id,
                ),
            )
        )
    rows = (
        await database.execute(
            select(OnlineDataRecord, OnlineDataBatch)
            .join(OnlineDataBatch, OnlineDataBatch.id == OnlineDataRecord.batch_id)
            .where(*filters)
            .order_by(OnlineDataRecord.occurred_at.desc(), OnlineDataRecord.id.desc())
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    next_cursor = visible_rows[-1][0].id if has_more and visible_rows else None
    return OnlineDataRecordPage(
        items=[record_response(record, batch) for record, batch in visible_rows],
        next_cursor=next_cursor,
        limit=limit,
    )


@router.get("/records/{record_id}", response_model=OnlineDataRecordDetail)
async def get_online_data_record(
    record_id: UUID,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> OnlineDataRecordDetail:
    row = (
        await database.execute(
            select(OnlineDataRecord, OnlineDataBatch)
            .join(OnlineDataBatch, OnlineDataBatch.id == OnlineDataRecord.batch_id)
            .where(OnlineDataRecord.id == record_id)
        )
    ).one_or_none()
    if row is None:
        raise online_data_error(404, "online_record_not_found", "线上回流记录不存在")
    record, batch = row
    base = record_response(record, batch)
    return OnlineDataRecordDetail(
        **base.model_dump(),
        prompt=record.prompt,
        answer=record.answer,
        trace=[OnlineTraceEvent.model_validate(event) for event in record.trace_json],
        metadata=record.metadata_json,
        content_hash=record.content_hash,
    )


@router.post("/records/batch-review", response_model=OnlineDataBatchOperationResponse)
async def batch_review_online_data_records(
    payload: OnlineDataBatchReviewInput,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> OnlineDataBatchOperationResponse:
    user = authorization.user
    if user is None:
        raise online_data_error(401, "user_missing", "登录已失效")
    record_ids = list(dict.fromkeys(payload.record_ids))
    records = list(
        await database.scalars(
            select(OnlineDataRecord).where(OnlineDataRecord.id.in_(record_ids))
        )
    )
    now = utc_now()
    for record in records:
        record.status = payload.status
        record.reviewed_by_user_id = None if user.is_shared else user.id
        record.reviewer_subject_key = user.subject_key
        record.reviewer_display_name = user.display_name
        record.review_note = payload.note.strip() if payload.note else None
        record.reviewed_at = now
    await database.commit()
    return OnlineDataBatchOperationResponse(
        requested=len(record_ids),
        updated=len(records),
        skipped=len(record_ids) - len(records),
    )
