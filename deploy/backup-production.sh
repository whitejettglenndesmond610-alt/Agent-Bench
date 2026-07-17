#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-${AGENT_EVAL_ENV_FILE:-$ROOT_DIR/.env.production}}"

if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Production environment file not found: %s\n' "$ENV_FILE" >&2
    exit 1
fi

set -a
# The durable operator file is trusted configuration and follows Compose env syntax.
source "$ENV_FILE"
set +a

BACKUP_DIR="${2:-${AGENT_EVAL_BACKUP_PATH:-}}"
: "${BACKUP_DIR:?Set AGENT_EVAL_BACKUP_PATH or pass a backup directory}"
: "${POSTGRES_USER:?Set POSTGRES_USER in the environment file}"
: "${POSTGRES_DB:?Set POSTGRES_DB in the environment file}"
RETENTION_DAYS="${AGENT_EVAL_BACKUP_RETENTION_DAYS:-14}"
DEPLOYMENT_OVERLAY="${AGENT_EVAL_DEPLOYMENT_OVERLAY:-deploy/compose.production.yaml}"

case "$DEPLOYMENT_OVERLAY" in
    deploy/compose.production.yaml|deploy/compose.local-real.yaml) ;;
    *)
        printf '%s\n' 'Unsupported AGENT_EVAL_DEPLOYMENT_OVERLAY' >&2
        exit 1
        ;;
esac

if [[ "$BACKUP_DIR" != /* ]]; then
    printf '%s\n' 'Backup directory must be an absolute path' >&2
    exit 1
fi
install -d -m 0700 "$BACKUP_DIR"
AGENT_EVAL_BACKUP_PATH="$BACKUP_DIR"
export AGENT_EVAL_BACKUP_PATH

exec 9>"$BACKUP_DIR/.backup.lock"
if ! flock -n 9; then
    printf '%s\n' 'Another production backup is active' >&2
    exit 1
fi

compose=(
    docker compose
    --project-directory "$ROOT_DIR/deploy"
    --env-file "$ENV_FILE"
    -f "$ROOT_DIR/deploy/compose.yaml"
    -f "$ROOT_DIR/$DEPLOYMENT_OVERLAY"
    -f "$ROOT_DIR/deploy/compose.postgres.yaml"
    -f "$ROOT_DIR/deploy/compose.backup.yaml"
)
if [[ "${AGENT_EVAL_ARTIFACT_BACKEND:-local}" == "s3" &&
      "${AGENT_EVAL_ARTIFACT_S3_ENDPOINT_URL:-}" == *"://minio:"* ]]; then
    compose+=( -f "$ROOT_DIR/deploy/compose.minio.yaml" )
fi

set_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
database_name="backup-set-$set_id.postgres.dump"
artifacts_name="backup-set-$set_id.artifacts.tar.gz"
database_temp="$BACKUP_DIR/.$database_name.incomplete"
artifacts_temp="$BACKUP_DIR/.$artifacts_name.incomplete"
database_final="$BACKUP_DIR/$database_name"
artifacts_final="$BACKUP_DIR/$artifacts_name"
container_validation="/tmp/agent-eval-backup-$set_id.dump"

cleanup() {
    rm -f -- "$database_temp" "$artifacts_temp"
    "${compose[@]}" exec -T postgres rm -f -- "$container_validation" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" config --quiet
"${compose[@]}" exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null
"${compose[@]}" --profile tools run --rm --no-deps maintenance check >/dev/null

"${compose[@]}" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom >"$database_temp"
"${compose[@]}" cp "$database_temp" "postgres:$container_validation" >/dev/null
"${compose[@]}" exec -T postgres pg_restore --list "$container_validation" >/dev/null
"${compose[@]}" exec -T postgres rm -f -- "$container_validation"

"${compose[@]}" --profile tools run --rm --no-deps maintenance \
    export-artifact-archive "/backups/.$artifacts_name.incomplete" >/dev/null
"${compose[@]}" --profile tools run --rm --no-deps maintenance \
    verify-artifact-archive "/backups/.$artifacts_name.incomplete" >/dev/null
"${compose[@]}" --profile tools run --rm --no-deps --entrypoint chown maintenance \
    "$AGENT_EVAL_BACKUP_UID:$AGENT_EVAL_BACKUP_GID" \
    "/backups/.$artifacts_name.incomplete"

mv -- "$database_temp" "$database_final"
mv -- "$artifacts_temp" "$artifacts_final"
python3 "$ROOT_DIR/deploy/backup_sets.py" complete \
    "$BACKUP_DIR" "$set_id" "$database_name" "$artifacts_name"
python3 "$ROOT_DIR/deploy/backup_sets.py" retain "$BACKUP_DIR" "$RETENTION_DAYS"
trap - EXIT
printf 'backup_set=%s complete\n' "$set_id"
