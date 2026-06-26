#!/usr/bin/env bash
# Start debug-exec (:9000) + agent-server (:9100). The Redis stream buffer is an
# external ElastiCache cluster (endpoint baked at /redis_host) — no local redis.
set -uo pipefail

echo "[entrypoint] redis backend: $(cat /redis_host 2>/dev/null || echo '(none)')"

echo "[entrypoint] debug-exec :9000..."
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

echo "[entrypoint] agent-server :9100..."
/opt/venv/bin/python /agent-server.py >/var/log/agent-server.log 2>&1 &

echo "[entrypoint] ready"
exec tail -f /dev/null
