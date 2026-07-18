#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-}"
MANIFEST_PATH="${2:-}"

if [[ ! -f "$ENV_FILE" || ! -f "$MANIFEST_PATH" ]]; then
    printf 'Usage: %s REHEARSAL_ENV BACKUP_SET_MANIFEST\n' "$0" >&2
    exit 2
fi

set -a
source "$ENV_FILE"
set +a

: "${AGENT_EVAL_REHEARSAL_PROJECT:?Set a unique AGENT_EVAL_REHEARSAL_PROJECT}"
: "${AGENT_EVAL_BACKUP_PATH:?Set AGENT_EVAL_BACKUP_PATH to the backup directory}"
: "${POSTGRES_USER:?Set POSTGRES_USER}"
: "${POSTGRES_DB:?Set POSTGRES_DB}"
if [[ "$AGENT_EVAL_REHEARSAL_PROJECT" != agent-eval-rehearsal-* ]]; then
    printf '%s\n' 'Rehearsal project name must start with agent-eval-rehearsal-' >&2
    exit 1
fi

manifest_name="$(basename "$MANIFEST_PATH")"
if [[ "$manifest_name" != backup-set-*.json || "$(dirname "$(realpath "$MANIFEST_PATH")")" != "$(realpath "$AGENT_EVAL_BACKUP_PATH")" ]]; then
    printf '%s\n' 'Manifest must be a backup-set JSON file in AGENT_EVAL_BACKUP_PATH' >&2
    exit 1
fi

readarray -t components < <(python3 - "$MANIFEST_PATH" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
if manifest.get("format") != "agent-eval-backup-set" or manifest.get("version") != 1:
    raise SystemExit("invalid backup set manifest")
print(manifest["database"]["filename"])
print(manifest["artifacts"]["filename"])
PY
)
database_name="${components[0]}"
artifacts_name="${components[1]}"

compose=(
    docker compose
    --project-name "$AGENT_EVAL_REHEARSAL_PROJECT"
    --project-directory "$ROOT_DIR/deploy"
    --env-file "$ENV_FILE"
    -f "$ROOT_DIR/deploy/compose.yaml"
    -f "$ROOT_DIR/deploy/compose.postgres.yaml"
    -f "$ROOT_DIR/deploy/compose.backup.yaml"
)
uses_minio=false
if [[ "${AGENT_EVAL_ARTIFACT_BACKEND:-local}" == "s3" &&
      "${AGENT_EVAL_ARTIFACT_S3_ENDPOINT_URL:-}" == *"://minio:"* ]]; then
    compose+=( -f "$ROOT_DIR/deploy/compose.minio.yaml" )
    uses_minio=true
fi

# A unique Compose project creates a new PostgreSQL volume. Do not use a production project name.
"${compose[@]}" config --quiet
if "${compose[@]}" ps --all --services | grep -qx postgres; then
    printf '%s\n' 'Rehearsal project already exists; choose a new project name' >&2
    exit 1
fi
"${compose[@]}" --profile tools build migrate maintenance
"${compose[@]}" up -d --wait postgres
if [[ "$uses_minio" == true ]]; then
    "${compose[@]}" up -d --wait minio
    "${compose[@]}" run --rm minio-init
fi
"${compose[@]}" cp "$AGENT_EVAL_BACKUP_PATH/$database_name" postgres:/tmp/rehearsal.dump >/dev/null
"${compose[@]}" exec -T postgres pg_restore \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error /tmp/rehearsal.dump
"${compose[@]}" exec -T postgres rm -f /tmp/rehearsal.dump
"${compose[@]}" --profile tools run --rm --no-deps maintenance \
    restore-artifact-archive "/backups/$artifacts_name"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" run --rm --no-deps migrate alembic check
"${compose[@]}" --profile tools run --rm --no-deps maintenance check

printf '%s\n' 'Restore rehearsal passed. API, worker, and web were not started.'
printf 'Inspect the isolated project, then remove it manually with: docker compose -p %q down -v\n' \
    "$AGENT_EVAL_REHEARSAL_PROJECT"
