import json
import os
from typing import Any

from fastapi import FastAPI

app = FastAPI(title="Agent Evaluation Fake Services", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
async def run_agent(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", ""))
    variant = os.environ.get("FAKE_AGENT_VARIANT", "alpha")
    answer = fake_answer(query, variant)
    if variant == "alpha":
        return {"result": {"answer": answer, "sources": []}}
    return {"answer": answer, "citations": []}


@app.post("/chat/completions")
async def judge(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    system_prompt = ""
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            system_prompt = str(first.get("content", ""))
    if "support_score" in system_prompt or "引用" in system_prompt:
        verdict: dict[str, Any] = {
            "support_score": 80,
            "reason": "Fake Judge: source supports the answer",
        }
    elif "rating" in system_prompt or "1 到 5" in system_prompt:
        verdict = {"rating": 4, "reason": "Fake Judge: complete demo response"}
    else:
        verdict = {"verdict": "correct", "reason": "Fake Judge: accepted demo response"}
    return {
        "choices": [{"message": {"content": json.dumps(verdict, ensure_ascii=False)}}]
    }


def fake_answer(prompt: str, variant: str) -> str:
    if "只返回一个选项标识" in prompt:
        return "A" if variant == "alpha" else "B"
    if variant == "alpha":
        return "Fake Agent Alpha deterministic answer"
    return "Fake Agent Beta deterministic answer"
