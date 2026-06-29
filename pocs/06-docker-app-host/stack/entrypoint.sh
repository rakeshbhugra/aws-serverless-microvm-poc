#!/usr/bin/env bash
# MicroVM entrypoint (M1): debug-exec + dockerd + Redis (stream buffer) +
# agent-server. Claude is triggered on demand via POST /run (:9100); it fetches
# the credential from Secrets Manager itself. No `set -e` — keep the VM alive.
set -uo pipefail

echo "[entrypoint] debug-exec :9000..."
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

echo "[entrypoint] dockerd..."
dockerd >/var/log/dockerd.log 2>&1 &
for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 && echo "[entrypoint] dockerd up" || { echo "[entrypoint] dockerd FAILED"; tail -n 40 /var/log/dockerd.log; }

# Redis stream buffer as a container (avoids al2023 redis packaging; `docker run`
# DNS works via 127.0.0.2). M4 will point REDIS_HOST at a central ElastiCache instead.
echo "[entrypoint] redis (container, host net)..."
docker run -d --name redis --network=host --restart unless-stopped redis:7-alpine >/dev/null 2>&1 || echo "[entrypoint] redis run failed"
for _ in $(seq 1 30); do docker exec redis redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done

echo "[entrypoint] agent-server :9100..."
/opt/venv/bin/python /agent-server.py >/var/log/agent-server.log 2>&1 &

echo "[entrypoint] ready — POST /run on :9100 to start Claude; idling"
exec tail -f /dev/null
