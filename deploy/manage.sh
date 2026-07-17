#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${AGENT_EVAL_ENV_FILE:-$ROOT_DIR/.env.production}"

if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Production environment file not found: %s\n' "$ENV_FILE" >&2
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${AGENT_EVAL_DATA_PATH:?Set AGENT_EVAL_DATA_PATH in the production environment file}"
: "${AGENT_EVAL_SECRETS_PATH:?Set AGENT_EVAL_SECRETS_PATH in the production environment file}"
: "${AGENT_EVAL_DATASET_PATH:?Set AGENT_EVAL_DATASET_PATH in the production environment file}"

compose=(
    docker compose
    --project-directory "$ROOT_DIR"
    --env-file "$ENV_FILE"
    -f "$ROOT_DIR/deploy/compose.yaml"
    -f "$ROOT_DIR/deploy/compose.production.yaml"
)

maintenance() {
    "${compose[@]}" --profile tools build maintenance
    "${compose[@]}" --profile tools run --rm maintenance "$@"
}

case "${1:-}" in
    start)
        "${compose[@]}" config --quiet
        "${compose[@]}" up --build -d --wait
        ;;
    upgrade)
        "${compose[@]}" config --quiet
        "${compose[@]}" down
        if [[ -f "$AGENT_EVAL_DATA_PATH/agent-eval.db" ]]; then
            maintenance backup
        fi
        "${compose[@]}" up --build -d --wait
        ;;
    stop)
        "${compose[@]}" down
        ;;
    status)
        "${compose[@]}" ps
        ;;
    logs)
        "${compose[@]}" logs --no-color --tail=500 "${@:2}"
        ;;
    bootstrap)
        maintenance bootstrap --dataset /datasets/demo
        ;;
    preflight)
        shift
        maintenance preflight "$@"
        ;;
    backup)
        maintenance backup
        ;;
    verify-backup)
        maintenance verify-backup
        ;;
    restore)
        "${compose[@]}" down
        maintenance restore --confirm-stopped
        ;;
    cleanup-artifacts)
        maintenance cleanup-artifacts
        ;;
    *)
        printf '%s\n' \
            "Usage: deploy/manage.sh {start|upgrade|stop|status|logs|bootstrap|preflight|backup|verify-backup|restore|cleanup-artifacts}" >&2
        exit 2
        ;;
esac
