FROM ghcr.io/astral-sh/uv:0.11.25 AS uv

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# git is required to fetch the tau2 git dependency in the tau2 group.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group tau2 --no-install-project

COPY apps/api/src ./apps/api/src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --group tau2

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

CMD ["uvicorn", "agent_eval.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
