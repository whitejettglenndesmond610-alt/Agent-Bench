import hashlib
import json

from agent_eval.db.models import EvaluationCase, WorkspaceAttempt


def canonical_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def source_payload(attempt: WorkspaceAttempt) -> dict[str, object]:
    assistant = attempt.assistant_message
    version = attempt.agent_version
    return {
        "agent": {
            "agent_id": str(version.agent.id),
            "agent_version_id": str(version.id),
            "name": version.agent.name,
            "provider": version.agent.provider,
            "version": version.version,
        },
        "artifact_sha256": attempt.artifact.sha256 if attempt.artifact is not None else None,
        "assistant_message_id": str(assistant.id) if assistant is not None else None,
        "attempt_id": str(attempt.id),
        "candidate_answer": assistant.content if assistant is not None else None,
        "citations": list(assistant.citations or []) if assistant is not None else [],
        "comparison_id": (
            str(attempt.conversation.comparison_id)
            if attempt.conversation.comparison_id is not None
            else None
        ),
        "conversation_id": str(attempt.conversation_id),
        "generation_no": attempt.generation_no,
        "input_tokens": attempt.input_tokens,
        "latency_ms": attempt.latency_ms,
        "output_tokens": attempt.output_tokens,
        "prompt_format": attempt.prompt_format,
        "prompt_hash": attempt.prompt_hash,
        "total_tokens": attempt.total_tokens,
        "user_message": attempt.user_message.content,
        "user_message_id": str(attempt.user_message_id),
    }


def record_payload(case: EvaluationCase) -> dict[str, object]:
    return {
        "ability": case.ability.value if case.ability is not None else None,
        "difficulty": case.difficulty,
        "domain": case.domain,
        "language": case.language.value if case.language is not None else None,
        "prompt": case.prompt,
        "question_type": case.question_type.value,
        "reference_answer": case.reference_answer,
        "requires_citations": case.requires_citations,
        "schema_version": "workspace-evaluation-case-v1",
    }


def record_hash(case: EvaluationCase) -> str:
    return canonical_sha256(record_payload(case))


def normalized_prompt_hash(prompt: str) -> str:
    normalized = " ".join(prompt.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
