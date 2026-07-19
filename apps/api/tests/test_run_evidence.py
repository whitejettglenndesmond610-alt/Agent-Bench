import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from test_runs import (
    READ_ONLY_PASSWORD,
    RunFixture,
    login_operator,
    run_client,
    run_payload,
)

from agent_eval.db.enums import (
    ArtifactKind,
    AttemptStatus,
    CitationFetchStatus,
    ExecutionStatus,
)
from agent_eval.db.models import (
    ArtifactIndex,
    CitationRecord,
    ExecutionItem,
    HttpAttempt,
    RunAgent,
    ScoreRecord,
)
from agent_eval.db.session import session_scope


async def create_run(
    client: AsyncClient,
    fixture: RunFixture,
    mutation_headers: dict[str, str],
    *,
    agent_index: int = 0,
    name: str = "Evidence run",
) -> UUID:
    payload = run_payload(fixture, name)
    payload["agent_version_ids"] = [str(fixture.agent_version_ids[agent_index])]
    response = await client.post(
        "/api/v1/evaluation-runs",
        headers=mutation_headers,
        json=payload,
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


async def seed_evidence(
    app: FastAPI,
    run_id: UUID,
) -> tuple[dict[str, UUID], str]:
    now = datetime.now(UTC)
    candidate_answer = "A complete candidate answer"
    response_content = json.dumps({"answer": candidate_answer}).encode()
    response_key = f"tests/run-evidence/{uuid4()}/response.json"
    stored = app.state.artifact_store.put(response_key, response_content)

    async with session_scope(app.state.session_factory) as session:
        items = list(
            await session.scalars(
                select(ExecutionItem)
                .join(RunAgent, RunAgent.id == ExecutionItem.run_agent_id)
                .options(joinedload(ExecutionItem.question))
                .where(RunAgent.run_id == run_id)
            )
        )
        by_external_id = {item.question.external_id: item for item in items}
        response_artifact = ArtifactIndex(
            kind=ArtifactKind.AGENT_RESPONSE,
            relative_path=stored.relative_path,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            expires_at=now + timedelta(days=1),
        )
        judge_artifact = ArtifactIndex(
            kind=ArtifactKind.JUDGE_RESPONSE,
            relative_path=f"tests/run-evidence/{uuid4()}/judge.json",
            sha256="b" * 64,
            size_bytes=321,
            expires_at=now + timedelta(days=1),
        )
        citation_artifact = ArtifactIndex(
            kind=ArtifactKind.CITATION_CONTENT,
            relative_path=f"tests/run-evidence/{uuid4()}/citation.bin",
            sha256="c" * 64,
            size_bytes=654,
            expires_at=now + timedelta(days=1),
        )
        session.add_all([response_artifact, judge_artifact, citation_artifact])
        await session.flush()

        first = by_external_id["reasoning-1"]
        first.status = ExecutionStatus.SCORED
        first.answer_preview = candidate_answer
        first.answer_hash = hashlib.sha256(candidate_answer.encode()).hexdigest()
        first.attempt_count = 2
        first.latency_ms = 180
        first.artifact_id = response_artifact.id
        session.add_all(
            [
                HttpAttempt(
                    execution_item_id=first.id,
                    attempt_no=2,
                    status=AttemptStatus.SUCCEEDED,
                    started_at=now - timedelta(seconds=1),
                    finished_at=now,
                    status_code=200,
                    latency_ms=80,
                    input_tokens=12,
                    output_tokens=4,
                    total_tokens=16,
                ),
                HttpAttempt(
                    execution_item_id=first.id,
                    attempt_no=1,
                    status=AttemptStatus.FAILED,
                    started_at=now - timedelta(seconds=2),
                    finished_at=now - timedelta(seconds=1),
                    status_code=503,
                    latency_ms=100,
                    error_type="agent_server_error",
                ),
                ScoreRecord(
                    execution_item_id=first.id,
                    answer_score=Decimal("95.00"),
                    citation_score=Decimal("85.00"),
                    final_score=Decimal("92.00"),
                    judge_reason="The answer is correct and supported.",
                    judge_artifact_id=judge_artifact.id,
                ),
                CitationRecord(
                    execution_item_id=first.id,
                    position=1,
                    original_url="https://example.test/source-two",
                    fetch_status=CitationFetchStatus.UNREACHABLE,
                    judge_reason="The source could not be reached.",
                ),
                CitationRecord(
                    execution_item_id=first.id,
                    position=0,
                    original_url="https://example.test/source-one",
                    final_url="https://example.test/final-one",
                    fetch_status=CitationFetchStatus.FETCHED,
                    http_status=200,
                    authenticity_score=Decimal("90.00"),
                    support_score=Decimal("80.00"),
                    judge_reason="The source supports the answer.",
                    artifact_id=citation_artifact.id,
                    judge_artifact_id=judge_artifact.id,
                ),
            ]
        )

        failed = by_external_id["reasoning-2"]
        failed.status = ExecutionStatus.FAILED
        failed.error_type = "agent_timeout"
        failed.attempt_count = 1
        failed.latency_ms = 500
        session.add(
            ScoreRecord(
                execution_item_id=failed.id,
                answer_score=Decimal("0"),
                citation_score=None,
                final_score=Decimal("0"),
                judge_reason="Agent request failed.",
            )
        )

        retrieval = by_external_id["retrieval-1"]
        retrieval.status = ExecutionStatus.SCORED
        retrieval.answer_preview = "Answer"
        retrieval.answer_hash = hashlib.sha256(b"Answer").hexdigest()
        retrieval.attempt_count = 1
        retrieval.latency_ms = 75
        session.add(
            ScoreRecord(
                execution_item_id=retrieval.id,
                answer_score=Decimal("70"),
                citation_score=Decimal("60"),
                final_score=Decimal("67"),
                judge_reason="Partially correct.",
            )
        )

        return ({key: item.id for key, item in by_external_id.items()}, response_key)


async def test_run_evidence_requires_operator_and_scopes_items_to_run(tmp_path: Path) -> None:
    async with run_client(tmp_path) as (client, _, _, fixture):
        unauthenticated = await client.get(f"/api/v1/evaluation-runs/{uuid4()}/evidence")
        assert unauthenticated.status_code == 401
        unauthenticated_detail = await client.get(
            f"/api/v1/evaluation-runs/{uuid4()}/evidence/{uuid4()}"
        )
        assert unauthenticated_detail.status_code == 401

        read_only_login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://test"},
            json={"role": "read_only", "password": READ_ONLY_PASSWORD},
        )
        assert read_only_login.status_code == 200
        forbidden = await client.get(f"/api/v1/evaluation-runs/{uuid4()}/evidence")
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "operator_required"
        forbidden_detail = await client.get(
            f"/api/v1/evaluation-runs/{uuid4()}/evidence/{uuid4()}"
        )
        assert forbidden_detail.status_code == 403

        mutation_headers = await login_operator(client)
        first_run_id = await create_run(
            client, fixture, mutation_headers, name="First evidence run"
        )
        second_run_id = await create_run(
            client,
            fixture,
            mutation_headers,
            agent_index=1,
            name="Second evidence run",
        )
        first_list = await client.get(f"/api/v1/evaluation-runs/{first_run_id}/evidence")
        assert first_list.status_code == 200
        assert first_list.headers["cache-control"] == "private, no-store"
        first_item_id = first_list.json()["items"][0]["execution_item_id"]

        cross_run = await client.get(
            f"/api/v1/evaluation-runs/{second_run_id}/evidence/{first_item_id}"
        )
        assert cross_run.status_code == 404
        assert cross_run.json()["detail"]["code"] == "evidence_not_found"

        missing_run = await client.get(f"/api/v1/evaluation-runs/{uuid4()}/evidence")
        assert missing_run.status_code == 404
        assert missing_run.json()["detail"]["code"] == "run_not_found"


async def test_run_evidence_list_paginates_filters_and_omits_sensitive_detail(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        run_id = await create_run(client, fixture, mutation_headers)
        await seed_evidence(app, run_id)

        page = await client.get(
            f"/api/v1/evaluation-runs/{run_id}/evidence",
            params={"limit": 1, "offset": 1},
        )
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        assert page.json()["total"] == 3
        assert page.json()["limit"] == 1
        assert page.json()["offset"] == 1
        assert len(page.json()["items"]) == 1
        serialized_item = json.dumps(page.json()["items"][0])
        assert "prompt" not in page.json()["items"][0]
        assert "reference_answer" not in page.json()["items"][0]
        assert "relative_path" not in serialized_item
        assert "artifact" not in serialized_item

        async def filtered(**params: object) -> list[dict[str, object]]:
            response = await client.get(
                f"/api/v1/evaluation-runs/{run_id}/evidence",
                params=params,
            )
            assert response.status_code == 200
            return response.json()["items"]

        retrieval = await filtered(ability="retrieval")
        assert [item["question_external_id"] for item in retrieval] == ["retrieval-1"]
        english = await filtered(language="en")
        assert [item["question_external_id"] for item in english] == ["retrieval-1"]
        failures = await filtered(status="failed", error_type="agent_timeout")
        assert [item["question_external_id"] for item in failures] == ["reasoning-2"]
        high_scores = await filtered(min_score=90)
        assert [item["question_external_id"] for item in high_scores] == ["reasoning-1"]
        low_scores = await filtered(max_score=1)
        assert [item["question_external_id"] for item in low_scores] == ["reasoning-2"]
        run_agent_id = high_scores[0]["run_agent_id"]
        assert len(await filtered(run_agent_id=run_agent_id)) == 3
        assert await filtered(run_agent_id=str(uuid4())) == []

        invalid_range = await client.get(
            f"/api/v1/evaluation-runs/{run_id}/evidence",
            params={"min_score": 80, "max_score": 20},
        )
        assert invalid_range.status_code == 422
        assert invalid_range.json()["detail"]["code"] == "invalid_score_range"


async def test_run_evidence_detail_orders_evidence_and_degrades_artifact_failures(
    tmp_path: Path,
) -> None:
    async with run_client(tmp_path) as (client, app, _, fixture):
        mutation_headers = await login_operator(client)
        run_id = await create_run(client, fixture, mutation_headers)
        item_ids, response_key = await seed_evidence(app, run_id)
        detail_url = (
            f"/api/v1/evaluation-runs/{run_id}/evidence/{item_ids['reasoning-1']}"
        )

        response = await client.get(detail_url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        detail = response.json()
        assert detail["prompt"] == "Question one"
        assert detail["choices"] == [
            {"label": "A", "text": "One"},
            {"label": "B", "text": "Two"},
        ]
        assert detail["reference_answer"] == "A"
        assert detail["answer_preview"] == "A complete candidate answer"
        assert detail["candidate_answer"] == "A complete candidate answer"
        assert detail["candidate_answer_status"] == "available"
        assert [attempt["attempt_no"] for attempt in detail["attempts"]] == [1, 2]
        assert detail["attempts"][1]["total_tokens"] == 16
        assert detail["score"]["final_score"] == 92.0
        assert detail["score"]["judge_reason"] == "The answer is correct and supported."
        assert [citation["position"] for citation in detail["citations"]] == [0, 1]
        assert detail["citations"][0]["authenticity_score"] == 90.0
        assert detail["citations"][0]["content_artifact"]["kind"] == "citation_content"
        assert detail["response_artifact"]["kind"] == "agent_response"
        assert detail["response_artifact"]["retained"] is True
        assert "relative_path" not in json.dumps(detail)
        assert response_key not in json.dumps(detail)

        async with session_scope(app.state.session_factory) as session:
            item = await session.get(ExecutionItem, item_ids["reasoning-1"])
            assert item is not None and item.artifact_id is not None
            artifact = await session.get(ArtifactIndex, item.artifact_id)
            assert artifact is not None
            artifact.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = await client.get(detail_url)
        assert expired.status_code == 200
        assert expired.json()["candidate_answer"] is None
        assert expired.json()["candidate_answer_status"] == "expired"
        assert expired.json()["response_artifact"]["retained"] is False

        async with session_scope(app.state.session_factory) as session:
            item = await session.get(ExecutionItem, item_ids["reasoning-1"])
            assert item is not None and item.artifact_id is not None
            artifact = await session.get(ArtifactIndex, item.artifact_id)
            assert artifact is not None
            artifact.expires_at = datetime.now(UTC) + timedelta(days=1)
        assert app.state.artifact_store.delete(response_key) is True
        missing = await client.get(detail_url)
        assert missing.status_code == 200
        assert missing.json()["candidate_answer"] is None
        assert missing.json()["candidate_answer_status"] == "unavailable"

        app.state.artifact_store.put(response_key, b"corrupt")
        corrupt = await client.get(detail_url)
        assert corrupt.status_code == 200
        assert corrupt.json()["candidate_answer"] is None
        assert corrupt.json()["candidate_answer_status"] == "unavailable"
