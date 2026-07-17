#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_PATH="${1:-}"
ENV_FILE="${2:-}"

if [[ ! -d "$REPOSITORY_PATH/deploy" || ! -f "$ENV_FILE" ]]; then
    printf 'Usage: %s REPOSITORY_PATH PRODUCTION_ENV_FILE\n' "$0" >&2
    exit 2
fi
REPOSITORY_PATH="$(realpath "$REPOSITORY_PATH")"
ENV_FILE="$(realpath "$ENV_FILE")"
DOCKER_HOST_VALUE="${DOCKER_HOST:-unix:///run/podman/podman.sock}"
if [[ ! "$REPOSITORY_PATH" =~ ^/[A-Za-z0-9._/-]+$ || ! "$ENV_FILE" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    printf '%s\n' 'Paths may contain only ASCII letters, digits, dot, underscore, slash, and hyphen' >&2
    exit 1
fi
if [[ ! "$DOCKER_HOST_VALUE" =~ ^unix:///[A-Za-z0-9._/-]+$ ]]; then
    printf '%s\n' 'DOCKER_HOST must be a local Unix socket path' >&2
    exit 1
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/agent-eval"
install -d -m 0700 "$CONFIG_DIR"
install -d -m 0755 "$UNIT_DIR"
install -m 0644 "$SCRIPT_DIR/agent-eval-monitor.service" "$UNIT_DIR/agent-eval-monitor.service"
install -m 0644 "$SCRIPT_DIR/agent-eval-monitor.timer" "$UNIT_DIR/agent-eval-monitor.timer"
umask 077
printf 'AGENT_EVAL_REPOSITORY_PATH=%s\nAGENT_EVAL_ENV_FILE=%s\nDOCKER_HOST=%s\n' \
    "$REPOSITORY_PATH" "$ENV_FILE" "$DOCKER_HOST_VALUE" >"$CONFIG_DIR/monitor.conf"

systemctl --user daemon-reload
systemctl --user enable --now agent-eval-monitor.timer
systemctl --user status agent-eval-monitor.timer --no-pager
