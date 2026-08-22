# Agent Evaluation Platform

## Layout

- Python package: `apps/api/src/agent_eval`; FastAPI entrypoint is `agent_eval.api.main:app` and the worker entrypoint is `agent_eval.worker.main`.
- The only pnpm workspace package is the React/Vite app in `apps/web`; it calls same-origin `/api/v1/*` endpoints through Vite's proxy in development.
- Database schema changes belong in Alembic revisions under `migrations/versions/`; run migrations before starting the API or worker.
- Evaluation runs are asynchronous: start `uv run python -m agent_eval.worker.main` separately from the API. API, worker, and maintenance commands must use the same database and artifact directory.

## Local Commands

- Prerequisites are Python 3.12, `uv`, Node 24, and pnpm 11. Install with `uv sync` and `pnpm install`.
- Backend API: `uv run alembic upgrade head` then `uv run uvicorn agent_eval.api.main:app --reload`. Vite: `pnpm dev:web` on port 5173, proxying to port 8000.
- Full verification order: `uv run ruff check .`, `uv run mypy`, `uv run pytest`, `pnpm lint:web`, `pnpm test:web`, `pnpm build:web`.
- Focused tests: `uv run pytest apps/api/tests/test_runs.py`; `pnpm --filter @agent-eval/web test -- src/App.test.tsx`.
- PostgreSQL migration/concurrency coverage is skipped unless `AGENT_EVAL_TEST_POSTGRES_URL` points to a disposable database; that test downgrades it to `base` and cleans it afterward.

## Configuration And Data

- Settings load `.env` with the `AGENT_EVAL_` prefix. Copy values from `.env.example`; generate password hashes and the token encryption key with the documented maintenance commands. Quote Argon2 hashes in `.env` because they contain `$`.
- `AGENT_EVAL_ARTIFACT_BACKEND` currently supports only `local`; keep artifact storage shared across API and every worker. Losing `AGENT_EVAL_ENCRYPTION_KEY` prevents decryption of stored Agent tokens.
- `data/demo-dataset/`, `data/balanced-v2-candidate/`, and `data/official-v4/` are ignored because they contain restricted benchmark material. Do not commit or redistribute them; use `import-balanced-v2` for candidate data and `import-official-v4` for the Official V4 cohort.

## Deployment

- The base Compose stack is local-only and binds web to loopback. For production, use `deploy/manage.sh` with `.env.production` and its base-plus-production Compose overlay; do not expose the base stack directly.
