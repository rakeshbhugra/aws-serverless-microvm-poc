#!/usr/bin/env bash
# Start Redis (the stream buffer) + debug-exec (:9000) + agent-server (:9100).
set -uo pipefail

echo "[entrypoint] redis..."
redis-server --daemonize yes

echo "[entrypoint] debug-exec :9000..."
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

echo "[entrypoint] agent-server :9100..."
/opt/venv/bin/python /agent-server.py >/var/log/agent-server.log 2>&1 &

echo "[entrypoint] ready"
exec tail -f /dev/null
