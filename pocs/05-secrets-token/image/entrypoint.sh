#!/usr/bin/env bash
# Start debug-exec (:9000) + agent-server (:9100). The Redis stream buffer is external
# ElastiCache (endpoint baked at /redis_host) reached over the VPC egress connector —
# no local redis.
set -uo pipefail

# Non-secret config for the in-VM Secrets Manager fetch. The secret *name* is not
# sensitive; its value is read at runtime via the VM's executionRole credentials.
export SECRET_NAME="${SECRET_NAME:-microvm/claude-api-key}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "[entrypoint] redis backend: $(cat /redis_host 2>/dev/null || echo '(none)')"

echo "[entrypoint] debug-exec :9000..."
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

echo "[entrypoint] agent-server :9100..."
/opt/venv/bin/python /agent-server.py >/var/log/agent-server.log 2>&1 &

echo "[entrypoint] ready"
exec tail -f /dev/null
