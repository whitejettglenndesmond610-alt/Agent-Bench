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
# Node.js 24 runs the MCP filesystem server (npx) for MCPMark evaluation.
ARG NODE_VERSION=24.21.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl xz-utils ca-certificates \
    && arch=$(uname -m) \
    && case "$arch" in x86_64) node_arch=x64 ;; aarch64) node_arch=arm64 ;; *) exit 1 ;; esac \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group tau2 --group mcp --no-install-project

COPY apps/api/src ./apps/api/src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --group tau2 --group mcp

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

# Warm the npx cache for the pinned MCP filesystem server so the first episode
# does not pay the package download (cache lives in appuser's home).
RUN npx -y @modelcontextprotocol/server-filesystem@2025.12.18 --help >/dev/null 2>&1 || true

CMD ["uvicorn", "agent_eval.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
