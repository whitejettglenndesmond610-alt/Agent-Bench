import asyncio
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import case, func, or_, select

from agent_eval.agents.mapping import extract_answer
from agent_eval.api.dependencies import (
    DatabaseSession,
    OperatorCsrfSession,
    OperatorSession,
)
from agent_eval.api.schemas.quality import (
    BadPatternFindingDetail,
    BadPatternFindingListItem,
    BadPatternFindingReviewInput,
    BadPatternRescanResponse,
    BadPatternRuleCreate,
    BadPatternRuleResponse,
    BadPatternRuleUpdate,
    CalibrationLabelInput,
    CalibrationReportResponse,
    CalibrationSampleInput,
    CalibrationSampleResponse,
    CalibrationSummaryResponse,
    CalibrationTaskItem,
    CalibrationTaskList,
    QualityBatchOperationResponse,
    QualityDashboardResponse,
    QualityDashboardSummary,
    QualityFindingBatchReviewInput,
    QualityReviewerResponse,
    QualityReviewInput,
    QualityReviewResponse,
    QualityTaskBatchAssignInput,
    QualityTaskResponse,
    QualityTraceEvent,
)
from agent_eval.core.artifacts import ArtifactIntegrityError, read_indexed_artifact
from agent_eval.db.base import utc_now
from agent_eval.db.enums import (
    BadPatternFindingStatus,
    BadPatternSeverity,
    JudgeCalibrationAgreement,
    JudgeCalibrationStatus,
    QualityTaskStatus,
    SessionRole,
)
from agent_eval.db.models import (
    Agent,
    AgentVersion,
    ArtifactIndex,
    AuthUser,
    BadPatternFinding,
    BadPatternRule,
    Benchmark,
    CitationRecord,
    DatasetVersion,
    EvaluationRun,
    ExecutionItem,
    HttpAttempt,
    JudgeCalibrationLabel,
    QualityReview,
    QualityReviewTask,
    Question,
    RunAgent,
    ScoreRecord,
)
from agent_eval.quality.calibration import CalibrationSummary, summarize_calibration
from agent_eval.quality.detector import InvalidBadPatternRule, compile_pattern, scan_bad_patterns

router = APIRouter(prefix="/api/v1/quality", tags=["quality-review"])


async def load_execution_answer(
    request: Request,
    database: DatabaseSession,
    item: ExecutionItem,
    version: AgentVersion,
) -> str | None:
    if item.artifact_id is None:
        return item.answer_preview
    artifact = await database.get(ArtifactIndex, item.artifact_id)
    if artifact is None:
        return item.answer_preview
    try:
        content = await asyncio.to_thread(
            read_indexed_artifact,
            request.app.state.artifact_store,
            relative_path=artifact.relative_path,
            expected_sha256=artifact.sha256,
            expected_size=artifact.size_bytes,
        )
        return extract_answer(json.loads(content), version.answer_jsonpath)
    except (ArtifactIntegrityError, json.JSONDecodeError, ValueError, TypeError):
        return item.answer_preview


def review_response(review: QualityReview | None) -> QualityReviewResponse | None:
    if review is None:
        return None
    return QualityReviewResponse(
        id=review.id,
        execution_item_id=review.execution_item_id,
        verdict=review.verdict,
        tags=list(review.tags),
        note=review.note,
        reviewer_display_name=review.reviewer_display_name,
        reviewed_at=review.reviewed_at,
        updated_at=review.updated_at,
    )


def task_response(task: QualityReviewTask | None) -> QualityTaskResponse | None:
    if task is None:
        return None
    return QualityTaskResponse(
        id=task.id,
        execution_item_id=task.execution_item_id,
        status=task.status,
        priority=task.priority,
        assigned_to_user_id=task.assigned_to_user_id,
        assignee_display_name=task.assignee_display_name,
        assigned_at=task.assigned_at,
        claimed_at=task.claimed_at,
        completed_at=task.completed_at,
        revision=task.revision,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def finding_response(
    finding: BadPatternFinding,
    rule: BadPatternRule,
    item: ExecutionItem,
    run_agent: RunAgent,
    version: AgentVersion,
    agent: Agent,
    question: Question,
    benchmark: Benchmark,
    run: EvaluationRun,
    review: QualityReview | None,
    task: QualityReviewTask | None,
) -> BadPatternFindingListItem:
    return BadPatternFindingListItem(
        id=finding.id,
        execution_item_id=item.id,
        run_id=run.id,
        run_name=run.name,
        agent_name=agent.name,
        agent_version=version.version,
        question_external_id=question.external_id,
        benchmark_name=benchmark.name,
        rule_id=rule.id,
        rule_key=rule.key,
        rule_name=rule.name,
        category=rule.category,
        severity=rule.severity,
        status=finding.status,
        match_count=finding.match_count,
        snippet=finding.snippet,
        confidence=float(finding.confidence),
        answer_preview=item.answer_preview,
        error_type=item.error_type,
        reviewer_display_name=finding.reviewer_display_name,
        review_note=finding.review_note,
        reviewed_at=finding.reviewed_at,
        quality_review=review_response(review),
        task=task_response(task),
        created_at=finding.created_at,
        updated_at=finding.updated_at,
    )


def finding_query() -> Any:
    return (
        select(
            BadPatternFinding,
            BadPatternRule,
            ExecutionItem,
            RunAgent,
            AgentVersion,
            Agent,
            Question,
            Benchmark,
            EvaluationRun,
            QualityReview,
            QualityReviewTask,
        )
        .join(BadPatternRule, BadPatternRule.id == BadPatternFinding.rule_id)
        .join(ExecutionItem, ExecutionItem.id == BadPatternFinding.execution_item_id)
        .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
        .join(AgentVersion, AgentVersion.id == RunAgent.agent_version_id)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .join(Question, Question.id == ExecutionItem.question_id)
        .join(DatasetVersion, DatasetVersion.id == Question.dataset_version_id)
        .join(Benchmark, Benchmark.id == DatasetVersion.benchmark_id)
        .join(EvaluationRun, EvaluationRun.id == RunAgent.run_id)
        .outerjoin(QualityReview, QualityReview.execution_item_id == ExecutionItem.id)
        .outerjoin(QualityReviewTask, QualityReviewTask.execution_item_id == ExecutionItem.id)
    )


async def require_finding_context(database: DatabaseSession, finding_id: UUID) -> Any:
    row = (
        await database.execute(finding_query().where(BadPatternFinding.id == finding_id))
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "finding_not_found", "message": "Bad Pattern 命中不存在"},
        )
    return row


async def ensure_quality_task(
    database: DatabaseSession, execution_item_id: UUID
) -> QualityReviewTask:
    task = await database.scalar(
        select(QualityReviewTask).where(
            QualityReviewTask.execution_item_id == execution_item_id
        )
    )
    if task is None:
        task = QualityReviewTask(execution_item_id=execution_item_id)
        database.add(task)
        await database.flush()
    return task


async def synchronize_task_completion(
    database: DatabaseSession, execution_item_id: UUID
) -> QualityReviewTask:
    task = await ensure_quality_task(database, execution_item_id)
    open_findings = (
        await database.scalar(
            select(func.count())
            .select_from(BadPatternFinding)
            .where(
                BadPatternFinding.execution_item_id == execution_item_id,
                BadPatternFinding.status == BadPatternFindingStatus.OPEN,
            )
        )
    ) or 0
    if open_findings == 0:
        task.status = QualityTaskStatus.COMPLETED
        task.completed_at = utc_now()
    elif task.status == QualityTaskStatus.COMPLETED:
        task.status = (
            QualityTaskStatus.ASSIGNED
            if task.assigned_to_user_id is not None
            else QualityTaskStatus.UNASSIGNED
        )
        task.completed_at = None
    task.revision += 1
    return task


@router.get("/reviewers", response_model=list[QualityReviewerResponse])
async def list_quality_reviewers(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> list[QualityReviewerResponse]:
    reviewers = list(
        await database.scalars(
            select(AuthUser)
            .where(
                AuthUser.role == SessionRole.OPERATOR,
                AuthUser.is_shared.is_(False),
                AuthUser.disabled_at.is_(None),
            )
            .order_by(AuthUser.display_name, AuthUser.username)
        )
    )
    return [
        QualityReviewerResponse(
            id=reviewer.id,
            username=reviewer.username or reviewer.subject_key,
            display_name=reviewer.display_name,
        )
        for reviewer in reviewers
    ]


@router.post("/batch/tasks/assign", response_model=QualityBatchOperationResponse)
async def batch_assign_quality_tasks(
    payload: QualityTaskBatchAssignInput,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> QualityBatchOperationResponse:
    finding_ids = list(dict.fromkeys(payload.finding_ids))
    assignee: AuthUser | None = None
    if payload.assignee_user_id is not None:
        assignee = await database.get(AuthUser, payload.assignee_user_id)
        if (
            assignee is None
            or assignee.is_shared
            or assignee.disabled_at is not None
            or assignee.role != SessionRole.OPERATOR
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_reviewer", "message": "只能分配给可用的实名操作员"},
            )
    finding_items = (
        await database.execute(
            select(BadPatternFinding.id, BadPatternFinding.execution_item_id).where(
                BadPatternFinding.id.in_(finding_ids)
            )
        )
    ).all()
    item_ids = list(dict.fromkeys(execution_item_id for _, execution_item_id in finding_items))
    updated_item_ids: set[UUID] = set()
    now = utc_now()
    for execution_item_id in item_ids:
        task = await ensure_quality_task(database, execution_item_id)
        if task.status == QualityTaskStatus.COMPLETED:
            continue
        task.assigned_to_user_id = assignee.id if assignee else None
        task.assignee_subject_key = assignee.subject_key if assignee else None
        task.assignee_display_name = assignee.display_name if assignee else None
        task.assigned_at = now if assignee else None
        task.claimed_at = None
        task.status = (
            QualityTaskStatus.ASSIGNED if assignee else QualityTaskStatus.UNASSIGNED
        )
        if payload.priority is not None:
            task.priority = payload.priority
        task.revision += 1
        updated_item_ids.add(execution_item_id)
    await database.commit()
    updated = sum(
        execution_item_id in updated_item_ids for _, execution_item_id in finding_items
    )
    return QualityBatchOperationResponse(
        requested=len(finding_ids), updated=updated, skipped=len(finding_ids) - updated
    )


@router.post("/batch/findings/review", response_model=QualityBatchOperationResponse)
async def batch_review_quality_findings(
    payload: QualityFindingBatchReviewInput,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> QualityBatchOperationResponse:
    user = authorization.user
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "user_missing", "message": "登录已失效"}
        )
    finding_ids = list(dict.fromkeys(payload.finding_ids))
    findings = list(
        await database.scalars(
            select(BadPatternFinding).where(BadPatternFinding.id.in_(finding_ids))
        )
    )
    now = utc_now()
    execution_item_ids: set[UUID] = set()
    for finding in findings:
        finding.status = payload.status
        finding.review_note = payload.note.strip() if payload.note else None
        finding.reviewed_by_user_id = None if user.is_shared else user.id
        finding.reviewer_subject_key = user.subject_key
        finding.reviewer_display_name = user.display_name
        finding.reviewed_at = now
        execution_item_ids.add(finding.execution_item_id)
    await database.flush()
    for execution_item_id in execution_item_ids:
        await synchronize_task_completion(database, execution_item_id)
    await database.commit()
    return QualityBatchOperationResponse(
        requested=len(finding_ids),
        updated=len(findings),
        skipped=len(finding_ids) - len(findings),
    )


@router.post("/tasks/{execution_item_id}/claim", response_model=QualityTaskResponse)
async def claim_quality_task(
    execution_item_id: UUID,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> QualityTaskResponse:
    user = authorization.user
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "user_missing", "message": "登录已失效"}
        )
    if user.is_shared:
        raise HTTPException(
            status_code=409,
            detail={"code": "named_operator_required", "message": "共享操作员不能认领任务"},
        )
    if await database.get(ExecutionItem, execution_item_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "execution_item_not_found", "message": "执行项不存在"},
        )
    task = await ensure_quality_task(database, execution_item_id)
    if task.status == QualityTaskStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_completed", "message": "任务已经完成"},
        )
    now = utc_now()
    task.assigned_to_user_id = user.id
    task.assignee_subject_key = user.subject_key
    task.assignee_display_name = user.display_name
    task.assigned_at = task.assigned_at or now
    task.claimed_at = now
    task.status = QualityTaskStatus.IN_REVIEW
    task.revision += 1
    await database.commit()
    await database.refresh(task)
    response = task_response(task)
    assert response is not None
    return response


@router.get("/dashboard", response_model=QualityDashboardResponse)
async def get_quality_dashboard(
    database: DatabaseSession,
    _authorization: OperatorSession,
    finding_status: Annotated[BadPatternFindingStatus | None, Query(alias="status")] = None,
    severity: BadPatternSeverity | None = None,
    task_status: QualityTaskStatus | None = None,
    assignee_user_id: UUID | None = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> QualityDashboardResponse:
    filters = []
    if finding_status is not None:
        filters.append(BadPatternFinding.status == finding_status)
    if severity is not None:
        filters.append(BadPatternRule.severity == severity)
    if task_status is not None:
        filters.append(QualityReviewTask.status == task_status)
    if assignee_user_id is not None:
        filters.append(QualityReviewTask.assigned_to_user_id == assignee_user_id)
    if search:
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                Agent.name.ilike(term),
                Question.external_id.ilike(term),
                BadPatternRule.name.ilike(term),
                BadPatternFinding.snippet.ilike(term),
            )
        )
    query = finding_query().where(*filters)
    total = await database.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    severity_order = case(
        (BadPatternRule.severity == BadPatternSeverity.CRITICAL, 0),
        (BadPatternRule.severity == BadPatternSeverity.HIGH, 1),
        (BadPatternRule.severity == BadPatternSeverity.MEDIUM, 2),
        else_=3,
    )
    rows = (
        await database.execute(
            query.order_by(severity_order, BadPatternFinding.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    status_rows = (
        await database.execute(
            select(BadPatternFinding.status, func.count()).group_by(BadPatternFinding.status)
        )
    ).all()
    status_counts: dict[BadPatternFindingStatus, int] = {
        finding_status: count for finding_status, count in status_rows
    }
    critical_open = await database.scalar(
        select(func.count())
        .select_from(BadPatternFinding)
        .join(BadPatternRule)
        .where(
            BadPatternFinding.status == BadPatternFindingStatus.OPEN,
            BadPatternRule.severity == BadPatternSeverity.CRITICAL,
        )
    )
    task_status_rows = (
        await database.execute(
            select(QualityReviewTask.status, func.count()).group_by(QualityReviewTask.status)
        )
    ).all()
    task_counts: dict[QualityTaskStatus, int] = {
        task_status: count for task_status, count in task_status_rows
    }
    return QualityDashboardResponse(
        summary=QualityDashboardSummary(
            total_findings=sum(status_counts.values()),
            open_findings=status_counts.get(BadPatternFindingStatus.OPEN, 0),
            critical_open_findings=critical_open or 0,
            confirmed_findings=status_counts.get(BadPatternFindingStatus.CONFIRMED, 0),
            dismissed_findings=status_counts.get(BadPatternFindingStatus.DISMISSED, 0),
            reviewed_items=(await database.scalar(select(func.count()).select_from(QualityReview)))
            or 0,
            enabled_rules=(
                await database.scalar(
                    select(func.count())
                    .select_from(BadPatternRule)
                    .where(BadPatternRule.enabled.is_(True))
                )
            )
            or 0,
            unassigned_tasks=task_counts.get(QualityTaskStatus.UNASSIGNED, 0),
            active_tasks=(
                task_counts.get(QualityTaskStatus.ASSIGNED, 0)
                + task_counts.get(QualityTaskStatus.IN_REVIEW, 0)
            ),
            completed_tasks=task_counts.get(QualityTaskStatus.COMPLETED, 0),
        ),
        findings=[finding_response(*row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/feedback-export")
async def export_quality_feedback(
    request: Request,
    database: DatabaseSession,
    _authorization: OperatorSession,
    severity: BadPatternSeverity | None = None,
    assignee_user_id: UUID | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
) -> Response:
    filters = [BadPatternFinding.status == BadPatternFindingStatus.CONFIRMED]
    if severity is not None:
        filters.append(BadPatternRule.severity == severity)
    if assignee_user_id is not None:
        filters.append(QualityReviewTask.assigned_to_user_id == assignee_user_id)
    rows = (
        await database.execute(
            finding_query()
            .where(*filters)
            .order_by(BadPatternFinding.reviewed_at.desc())
            .limit(limit)
        )
    ).all()
    exported_at = utc_now()
    lines: list[str] = []
    for row in rows:
        finding, rule, item, _, version, agent, question, benchmark, run, review, task = row
        lines.append(
            json.dumps(
                {
                    "schema_version": "quality-feedback.v1",
                    "exported_at": exported_at.isoformat(),
                    "finding_id": str(finding.id),
                    "execution_item_id": str(item.id),
                    "run": {"id": str(run.id), "name": run.name},
                    "agent": {"id": str(agent.id), "name": agent.name, "version": version.version},
                    "benchmark": benchmark.name,
                    "question": {
                        "external_id": question.external_id,
                        "prompt": question.prompt,
                        "reference_answer": question.reference_answer,
                    },
                    "answer": await load_execution_answer(request, database, item, version),
                    "bad_pattern": {
                        "rule_key": rule.key,
                        "rule_version": rule.version,
                        "category": rule.category,
                        "severity": rule.severity.value,
                        "match_count": finding.match_count,
                        "snippet": finding.snippet,
                        "status": finding.status.value,
                        "review_note": finding.review_note,
                        "reviewer": finding.reviewer_display_name,
                        "reviewed_at": (
                            finding.reviewed_at.isoformat() if finding.reviewed_at else None
                        ),
                    },
                    "quality_review": (
                        {
                            "verdict": review.verdict.value,
                            "tags": list(review.tags),
                            "note": review.note,
                            "reviewer": review.reviewer_display_name,
                            "reviewed_at": review.reviewed_at.isoformat(),
                        }
                        if review
                        else None
                    ),
                    "task": (
                        {
                            "status": task.status.value,
                            "priority": task.priority.value,
                            "assignee": task.assignee_display_name,
                        }
                        if task
                        else None
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    filename = f"quality-feedback-{exported_at:%Y%m%d-%H%M%S}.jsonl"
    return Response(
        content="\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/findings/{finding_id}", response_model=BadPatternFindingDetail)
async def get_quality_finding(
    finding_id: UUID,
    request: Request,
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> BadPatternFindingDetail:
    row = await require_finding_context(database, finding_id)
    finding, rule, item, run_agent, version, agent, question, benchmark, run, review, task = row
    attempts = list(
        await database.scalars(
            select(HttpAttempt)
            .where(HttpAttempt.execution_item_id == item.id)
            .order_by(HttpAttempt.attempt_no)
        )
    )
    score = await database.scalar(
        select(ScoreRecord).where(ScoreRecord.execution_item_id == item.id)
    )
    citations = list(
        await database.scalars(
            select(CitationRecord)
            .where(CitationRecord.execution_item_id == item.id)
            .order_by(CitationRecord.position)
        )
    )
    trace = [
        QualityTraceEvent(
            kind="request",
            title=f"HTTP Attempt #{attempt.attempt_no}",
            status=attempt.status.value,
            detail=(f"HTTP {attempt.status_code}" if attempt.status_code else attempt.error_type),
            occurred_at=attempt.started_at,
            latency_ms=attempt.latency_ms,
        )
        for attempt in attempts
    ]
    trace.append(
        QualityTraceEvent(
            kind="response",
            title="Agent 回答",
            status=item.status.value,
            detail=item.answer_preview,
            occurred_at=item.updated_at,
            latency_ms=item.latency_ms,
        )
    )
    if score is not None:
        trace.append(
            QualityTraceEvent(
                kind="judge",
                title=f"Judge 评分 {float(score.final_score):.2f}",
                status="scored",
                detail=score.judge_reason,
                occurred_at=score.updated_at,
            )
        )
    trace.extend(
        QualityTraceEvent(
            kind="citation",
            title=f"引用 #{citation.position + 1}",
            status=citation.fetch_status.value,
            detail=citation.final_url or citation.original_url,
            occurred_at=citation.updated_at,
        )
        for citation in citations
    )
    trace.append(
        QualityTraceEvent(
            kind="detector",
            title=rule.name,
            status=finding.status.value,
            detail=finding.snippet,
            occurred_at=finding.created_at,
        )
    )
    if review is not None:
        trace.append(
            QualityTraceEvent(
                kind="human_review",
                title=f"人工质检：{review.verdict.value}",
                status=review.verdict.value,
                detail=review.note,
                occurred_at=review.reviewed_at,
            )
        )
    base = finding_response(*row)
    return BadPatternFindingDetail(
        **base.model_dump(),
        prompt=question.prompt,
        reference_answer=question.reference_answer,
        answer=await load_execution_answer(request, database, item, version),
        final_score=float(score.final_score) if score else None,
        judge_reason=score.judge_reason if score else None,
        trace=trace,
    )


@router.patch("/findings/{finding_id}", response_model=BadPatternFindingListItem)
async def review_quality_finding(
    finding_id: UUID,
    payload: BadPatternFindingReviewInput,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> BadPatternFindingListItem:
    row = await require_finding_context(database, finding_id)
    finding = row[0]
    user = authorization.user
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "user_missing", "message": "登录已失效"}
        )
    finding.status = payload.status
    finding.review_note = payload.note.strip() if payload.note else None
    finding.reviewed_by_user_id = None if user.is_shared else user.id
    finding.reviewer_subject_key = user.subject_key
    finding.reviewer_display_name = user.display_name
    finding.reviewed_at = utc_now()
    await database.flush()
    await synchronize_task_completion(database, finding.execution_item_id)
    await database.commit()
    updated = await require_finding_context(database, finding_id)
    return finding_response(*updated)


@router.put("/execution-items/{execution_item_id}/review", response_model=QualityReviewResponse)
async def upsert_quality_review(
    execution_item_id: UUID,
    payload: QualityReviewInput,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> QualityReviewResponse:
    item = await database.get(ExecutionItem, execution_item_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail={"code": "execution_item_not_found", "message": "执行项不存在"}
        )
    user = authorization.user
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "user_missing", "message": "登录已失效"}
        )
    review = await database.scalar(
        select(QualityReview).where(QualityReview.execution_item_id == execution_item_id)
    )
    if review is None:
        review = QualityReview(execution_item_id=execution_item_id)
        database.add(review)
    review.verdict = payload.verdict
    review.tags = payload.tags
    review.note = payload.note.strip() if payload.note else None
    review.reviewed_by_user_id = None if user.is_shared else user.id
    review.reviewer_subject_key = user.subject_key
    review.reviewer_display_name = user.display_name
    review.reviewed_at = utc_now()
    task = await ensure_quality_task(database, execution_item_id)
    task.status = QualityTaskStatus.COMPLETED
    task.completed_at = review.reviewed_at
    task.revision += 1
    await database.commit()
    await database.refresh(review)
    response = review_response(review)
    assert response is not None
    return response


@router.post("/execution-items/{execution_item_id}/rescan", response_model=BadPatternRescanResponse)
async def rescan_execution_item(
    execution_item_id: UUID,
    request: Request,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> BadPatternRescanResponse:
    row = (
        await database.execute(
            select(ExecutionItem, AgentVersion)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .join(AgentVersion, AgentVersion.id == RunAgent.agent_version_id)
            .where(ExecutionItem.id == execution_item_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "answer_not_found", "message": "没有可重新检测的回答"}
        )
    item, version = row
    answer = await load_execution_answer(request, database, item, version)
    if not answer:
        raise HTTPException(
            status_code=404, detail={"code": "answer_not_found", "message": "没有可重新检测的回答"}
        )
    findings = await scan_bad_patterns(
        database, execution_item_id=execution_item_id, answer=answer
    )
    await database.commit()
    return BadPatternRescanResponse(
        execution_item_id=execution_item_id, finding_count=len(findings)
    )


@router.get("/rules", response_model=list[BadPatternRuleResponse])
async def list_bad_pattern_rules(
    database: DatabaseSession,
    _authorization: OperatorSession,
) -> list[BadPatternRuleResponse]:
    severity_order = case(
        (BadPatternRule.severity == BadPatternSeverity.CRITICAL, 0),
        (BadPatternRule.severity == BadPatternSeverity.HIGH, 1),
        (BadPatternRule.severity == BadPatternSeverity.MEDIUM, 2),
        else_=3,
    )
    rows = (
        await database.execute(
            select(
                BadPatternRule,
                func.count(BadPatternFinding.id),
                func.sum(
                    case((BadPatternFinding.status == BadPatternFindingStatus.OPEN, 1), else_=0)
                ),
            )
            .outerjoin(BadPatternFinding)
            .group_by(BadPatternRule.id)
            .order_by(BadPatternRule.enabled.desc(), severity_order, BadPatternRule.key)
        )
    ).all()
    return [
        BadPatternRuleResponse(
            id=rule.id,
            key=rule.key,
            name=rule.name,
            description=rule.description,
            category=rule.category,
            severity=rule.severity,
            pattern=rule.pattern,
            enabled=rule.enabled,
            version=rule.version,
            finding_count=count or 0,
            open_count=open_count or 0,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )
        for rule, count, open_count in rows
    ]


@router.post("/rules", response_model=BadPatternRuleResponse, status_code=201)
async def create_bad_pattern_rule(
    payload: BadPatternRuleCreate,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> BadPatternRuleResponse:
    try:
        compile_pattern(payload.pattern)
    except InvalidBadPatternRule as error:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_pattern", "message": str(error)}
        ) from error
    if await database.scalar(select(BadPatternRule.id).where(BadPatternRule.key == payload.key)):
        raise HTTPException(
            status_code=409, detail={"code": "rule_exists", "message": "规则 key 已存在"}
        )
    rule = BadPatternRule(
        **payload.model_dump(),
        created_by_user_id=authorization.user_id,
    )
    database.add(rule)
    await database.commit()
    await database.refresh(rule)
    return BadPatternRuleResponse(
        **{
            column: getattr(rule, column)
            for column in (
                "id",
                "key",
                "name",
                "description",
                "category",
                "severity",
                "pattern",
                "enabled",
                "version",
                "created_at",
                "updated_at",
            )
        }
    )


@router.patch("/rules/{rule_id}", response_model=BadPatternRuleResponse)
async def update_bad_pattern_rule(
    rule_id: UUID,
    payload: BadPatternRuleUpdate,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> BadPatternRuleResponse:
    rule = await database.get(BadPatternRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=404, detail={"code": "rule_not_found", "message": "规则不存在"}
        )
    updates = payload.model_dump(exclude_unset=True)
    if "pattern" in updates:
        try:
            compile_pattern(updates["pattern"])
        except InvalidBadPatternRule as error:
            raise HTTPException(
                status_code=422, detail={"code": "invalid_pattern", "message": str(error)}
            ) from error
    for key, value in updates.items():
        setattr(rule, key, value)
    rule.version += 1
    await database.commit()
    await database.refresh(rule)
    counts = await database.execute(
        select(
            func.count(BadPatternFinding.id),
            func.sum(case((BadPatternFinding.status == BadPatternFindingStatus.OPEN, 1), else_=0)),
        ).where(BadPatternFinding.rule_id == rule.id)
    )
    total, open_count = counts.one()
    return BadPatternRuleResponse(
        id=rule.id,
        key=rule.key,
        name=rule.name,
        description=rule.description,
        category=rule.category,
        severity=rule.severity,
        pattern=rule.pattern,
        enabled=rule.enabled,
        version=rule.version,
        finding_count=total or 0,
        open_count=open_count or 0,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def calibration_snapshot(score: ScoreRecord) -> dict[str, Any]:
    return {
        "answer_score": float(score.answer_score),
        "judge_reason": score.judge_reason,
        "judge_provider": score.judge_provider,
        "judge_model_requested": score.judge_model_requested,
        "judge_model_returned": score.judge_model_returned,
        "judge_prompt_version": score.judge_prompt_version,
        "judge_temperature": (
            float(score.judge_temperature) if score.judge_temperature is not None else None
        ),
        "details": score.judge_details_json,
    }


def calibration_context_query() -> Any:
    return (
        select(
            JudgeCalibrationLabel,
            ScoreRecord,
            ExecutionItem,
            RunAgent,
            EvaluationRun,
            AgentVersion,
            Agent,
            Question,
            Benchmark,
        )
        .join(ScoreRecord, ScoreRecord.id == JudgeCalibrationLabel.score_record_id)
        .join(ExecutionItem, ExecutionItem.id == ScoreRecord.execution_item_id)
        .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
        .join(EvaluationRun, EvaluationRun.id == RunAgent.run_id)
        .join(AgentVersion, AgentVersion.id == RunAgent.agent_version_id)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .join(Question, Question.id == ExecutionItem.question_id)
        .join(DatasetVersion, DatasetVersion.id == Question.dataset_version_id)
        .join(Benchmark, Benchmark.id == DatasetVersion.benchmark_id)
    )


def calibration_task_item(
    task: JudgeCalibrationLabel,
    score: ScoreRecord,
    item: ExecutionItem,
    run_agent: RunAgent,
    run: EvaluationRun,
    version: AgentVersion,
    agent: Agent,
    question: Question,
    benchmark: Benchmark,
) -> CalibrationTaskItem:
    return CalibrationTaskItem(
        id=task.id,
        score_record_id=score.id,
        execution_item_id=item.id,
        run_id=run.id,
        run_name=run.name,
        agent_name=agent.name,
        question_external_id=question.external_id,
        question_type=question.type,
        ability=benchmark.ability,
        judge_score=float(task.judge_score),
        judge_details=task.judge_details_json,
        status=task.status,
        human_label=task.human_label,
        note=task.note,
        labeler_display_name=task.labeler_display_name,
        labeled_at=task.labeled_at,
        created_at=task.created_at,
    )


def calibration_summary_response(summary: CalibrationSummary) -> CalibrationSummaryResponse:
    return CalibrationSummaryResponse(
        labeled=summary.labeled,
        agree=summary.agree,
        disagree=summary.disagree,
        agreement=summary.agreement,
        kappa=summary.kappa,
        insufficient_sample=summary.insufficient_sample,
    )


@router.post("/calibration/sample", response_model=CalibrationSampleResponse)
async def sample_calibration_tasks(
    payload: CalibrationSampleInput,
    database: DatabaseSession,
    _authorization: OperatorCsrfSession,
) -> CalibrationSampleResponse:
    if payload.run_id is not None:
        if (
            await database.scalar(
                select(EvaluationRun.id).where(EvaluationRun.id == payload.run_id)
            )
            is None
        ):
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": "评测任务不存在"},
            )
        run_ids = [payload.run_id]
    else:
        run_ids = list(
            await database.scalars(
                select(EvaluationRun.id)
                .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
                .limit(payload.recent_run_count)
            )
        )
    if not run_ids:
        return CalibrationSampleResponse(requested=payload.sample_size, eligible=0, created=0)

    eligible_filter = [
        RunAgent.run_id.in_(run_ids),
        JudgeCalibrationLabel.id.is_(None),
    ]
    eligible = (
        await database.scalar(
            select(func.count())
            .select_from(ScoreRecord)
            .join(ExecutionItem, ExecutionItem.id == ScoreRecord.execution_item_id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .outerjoin(
                JudgeCalibrationLabel,
                JudgeCalibrationLabel.score_record_id == ScoreRecord.id,
            )
            .where(*eligible_filter)
        )
    ) or 0
    candidates = list(
        await database.scalars(
            select(ScoreRecord)
            .join(ExecutionItem, ExecutionItem.id == ScoreRecord.execution_item_id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .outerjoin(
                JudgeCalibrationLabel,
                JudgeCalibrationLabel.score_record_id == ScoreRecord.id,
            )
            .where(*eligible_filter)
            .order_by(func.random())
            .limit(payload.sample_size)
        )
    )
    for score in candidates:
        database.add(
            JudgeCalibrationLabel(
                score_record_id=score.id,
                judge_score=score.answer_score,
                judge_details_json=calibration_snapshot(score),
            )
        )
    await database.commit()
    return CalibrationSampleResponse(
        requested=payload.sample_size, eligible=eligible, created=len(candidates)
    )


@router.get("/calibration/tasks", response_model=CalibrationTaskList)
async def list_calibration_tasks(
    database: DatabaseSession,
    _authorization: OperatorSession,
    calibration_status: Annotated[JudgeCalibrationStatus | None, Query(alias="status")] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CalibrationTaskList:
    query = calibration_context_query()
    if calibration_status is not None:
        query = query.where(JudgeCalibrationLabel.status == calibration_status)
    total = await database.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (
        await database.execute(
            query.order_by(JudgeCalibrationLabel.created_at.desc(), JudgeCalibrationLabel.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return CalibrationTaskList(
        items=[calibration_task_item(*row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post("/calibration/tasks/{task_id}/label", response_model=CalibrationTaskItem)
async def label_calibration_task(
    task_id: UUID,
    payload: CalibrationLabelInput,
    database: DatabaseSession,
    authorization: OperatorCsrfSession,
) -> CalibrationTaskItem:
    task = await database.get(JudgeCalibrationLabel, task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "calibration_task_not_found", "message": "校准任务不存在"},
        )
    if task.status != JudgeCalibrationStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail={"code": "calibration_task_labeled", "message": "校准任务已标注"},
        )
    user = authorization.user
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "user_missing", "message": "登录已失效"}
        )
    task.status = JudgeCalibrationStatus.LABELED
    task.human_label = payload.label
    task.note = payload.note.strip() if payload.note else None
    task.labeled_by_user_id = None if user.is_shared else user.id
    task.labeler_subject_key = user.subject_key
    task.labeler_display_name = user.display_name
    task.labeled_at = utc_now()
    await database.commit()
    row = (
        await database.execute(
            calibration_context_query().where(JudgeCalibrationLabel.id == task_id)
        )
    ).one()
    return calibration_task_item(*row)


@router.get("/calibration/report", response_model=CalibrationReportResponse)
async def get_calibration_report(
    database: DatabaseSession,
    _authorization: OperatorSession,
    run_id: UUID | None = None,
) -> CalibrationReportResponse:
    filters = [JudgeCalibrationLabel.status == JudgeCalibrationStatus.LABELED]
    if run_id is not None:
        filters.append(RunAgent.run_id == run_id)
    rows = (
        await database.execute(
            select(
                JudgeCalibrationLabel.judge_score,
                JudgeCalibrationLabel.human_label,
                ScoreRecord.judge_prompt_version,
                Question.type,
                Benchmark.ability,
            )
            .join(ScoreRecord, ScoreRecord.id == JudgeCalibrationLabel.score_record_id)
            .join(ExecutionItem, ExecutionItem.id == ScoreRecord.execution_item_id)
            .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
            .join(Question, Question.id == ExecutionItem.question_id)
            .join(DatasetVersion, DatasetVersion.id == Question.dataset_version_id)
            .join(Benchmark, Benchmark.id == DatasetVersion.benchmark_id)
            .where(*filters)
        )
    ).all()

    def pair(row: Any) -> tuple[float, bool]:
        judge_score, human_label, _, _, _ = row
        return float(judge_score), human_label == JudgeCalibrationAgreement.AGREE

    by_question_type: dict[str, list[tuple[float, bool]]] = {}
    by_ability: dict[str, list[tuple[float, bool]]] = {}
    by_prompt_version: dict[str, list[tuple[float, bool]]] = {}
    for row in rows:
        _, _, prompt_version, question_type, ability = row
        by_question_type.setdefault(question_type.value, []).append(pair(row))
        by_ability.setdefault(ability.value, []).append(pair(row))
        by_prompt_version.setdefault(prompt_version or "unrecorded", []).append(pair(row))
    return CalibrationReportResponse(
        overall=calibration_summary_response(
            summarize_calibration([pair(row) for row in rows])
        ),
        by_question_type={
            key: calibration_summary_response(summarize_calibration(pairs))
            for key, pairs in sorted(by_question_type.items())
        },
        by_ability={
            key: calibration_summary_response(summarize_calibration(pairs))
            for key, pairs in sorted(by_ability.items())
        },
        by_prompt_version={
            key: calibration_summary_response(summarize_calibration(pairs))
            for key, pairs in sorted(by_prompt_version.items())
        },
    )
