#!/usr/bin/env bash
# Start debug-exec (:9000), agent-server (:9100), and the lifecycle-hooks server
# (:$HOOKS_PORT, default 8080). INTERNET_EGRESS, so Secrets Manager, the Claude API, and
# S3 are reached over default egress. persist.py imports live at / (agent-server + hooks).
set -uo pipefail

# Non-secret config (names + region + S3 target). The VM reads the Claude credential and
# writes/reads run snapshots at runtime via its executionRole credentials.
export SECRET_NAME="${SECRET_NAME:-microvm/claude-api-key}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export SNAPSHOT_BUCKET="${SNAPSHOT_BUCKET:-}"
export SNAPSHOT_PREFIX="${SNAPSHOT_PREFIX:-microvm-runs}"
export SNAPSHOT_PATHS="${SNAPSHOT_PATHS:-/home/node/.claude/projects}"
export HOOKS_PORT="${HOOKS_PORT:-8080}"

echo "[entrypoint] snapshot bucket: ${SNAPSHOT_BUCKET:-'(unset)'} prefix=$SNAPSHOT_PREFIX paths=$SNAPSHOT_PATHS"

echo "[entrypoint] debug-exec :9000..."
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

echo "[entrypoint] hooks :$HOOKS_PORT..."
/opt/venv/bin/python /hooks.py >/var/log/hooks.log 2>&1 &

echo "[entrypoint] agent-server :9100..."
/opt/venv/bin/python /agent-server.py >/var/log/agent-server.log 2>&1 &

echo "[entrypoint] ready"
exec tail -f /dev/null
