#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-${AGENT_EVAL_ENV_FILE:-$ROOT_DIR/.env.production}}"

if [[ ! -f "$ENV_FILE" ]]; then
    printf '%s\n' 'FAILED: production environment file is unavailable' >&2
    exit 1
fi

set -a
# This durable operator-owned file follows Compose env syntax.
source "$ENV_FILE"
set +a

BACKUP_DIR="${AGENT_EVAL_BACKUP_PATH:-}"
: "${BACKUP_DIR:?Set AGENT_EVAL_BACKUP_PATH in the environment file}"
if [[ "$BACKUP_DIR" != /* || ! -d "$BACKUP_DIR" ]]; then
    printf '%s\n' 'FAILED: backup directory is unavailable' >&2
    exit 1
fi
DEPLOYMENT_OVERLAY="${AGENT_EVAL_DEPLOYMENT_OVERLAY:-deploy/compose.production.yaml}"
case "$DEPLOYMENT_OVERLAY" in
    deploy/compose.production.yaml|deploy/compose.local-real.yaml) ;;
    *)
        printf '%s\n' 'FAILED: unsupported deployment overlay' >&2
        exit 1
        ;;
esac

exec 9>"$BACKUP_DIR/.monitor.lock"
if ! flock -n 9; then
    printf '%s\n' 'FAILED: another production monitor is active' >&2
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
expected_services=(api worker web postgres)
if [[ "${AGENT_EVAL_ARTIFACT_BACKEND:-local}" == "s3" &&
      "${AGENT_EVAL_ARTIFACT_S3_ENDPOINT_URL:-}" == *"://minio:"* ]]; then
    compose+=( -f "$ROOT_DIR/deploy/compose.minio.yaml" )
    expected_services+=(minio)
fi

"${compose[@]}" config --quiet
for service in "${expected_services[@]}"; do
    mapfile -t containers < <("${compose[@]}" ps --status running -q "$service")
    if (( ${#containers[@]} == 0 )); then
        printf 'FAILED: expected service is not running: %s\n' "$service" >&2
        exit 1
    fi
done

readiness_url="${AGENT_EVAL_READINESS_URL:-http://127.0.0.1:8080/health/ready}"
if ! curl --fail --silent --max-time 10 --output /dev/null "$readiness_url" 2>/dev/null; then
    printf '%s\n' 'FAILED: public readiness check failed' >&2
    exit 1
fi

"${compose[@]}" --profile tools run --rm --no-deps maintenance operational-check
python3 "$ROOT_DIR/deploy/backup_sets.py" verify-latest \
    "$BACKUP_DIR" "${AGENT_EVAL_BACKUP_MAX_AGE_HOURS:-26}"
printf '%s\n' 'production_monitor=ok'
