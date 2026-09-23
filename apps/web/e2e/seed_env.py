"""Seed the E2E database with a fake Agent pointing at the local fake service.

Run via `uv run python apps/web/e2e/seed_env.py` with the backend environment
from apps/web/e2e/env.ts (AGENT_EVAL_DATABASE_URL and E2E_FAKE_AGENT_PORT must
be set). The seeded Agent mirrors agent_eval.maintenance.demo.FAKE_AGENTS but
targets 127.0.0.1 instead of the Compose service hostnames.
"""

import asyncio
import os

from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance import demo

fake_agent_port = os.environ.get("E2E_FAKE_AGENT_PORT", "9101")
demo.FAKE_AGENTS = (
    {
        "name": "Fake Agent Alpha",
        "provider": "Local Demo",
        "endpoint_url": f"http://127.0.0.1:{fake_agent_port}/run",
        "answer_jsonpath": "$.result.answer",
        "citations_jsonpath": "$.result.sources",
    },
)


async def main() -> None:
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            await demo.seed_fake_agents(session)
    finally:
        await engine.dispose()
    print(f"seeded fake Agent targeting 127.0.0.1:{fake_agent_port}")


if __name__ == "__main__":
    asyncio.run(main())
