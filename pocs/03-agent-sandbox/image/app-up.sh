#!/usr/bin/env bash
# Start the counter app directly (no Docker): redis + backend(:8000) + frontend(:3000).
# Idempotent-ish — safe to re-run; it just relaunches the servers.
set -uo pipefail

echo "[app] redis..."
redis-server --daemonize yes 2>/dev/null || true

echo "[app] backend :8000..."
(cd /workspace/backend && REDIS_HOST=localhost /opt/venv/bin/uvicorn app:app \
   --host 0.0.0.0 --port 8000 >/var/log/backend.log 2>&1 &)

echo "[app] frontend :3000..."
(cd /workspace/frontend && python3 -m http.server 3000 >/var/log/frontend.log 2>&1 &)

# wait for the backend to answer
for _ in $(seq 1 20); do
  curl -sf localhost:8000/count >/dev/null 2>&1 && break
  sleep 1
done
echo -n "[app] backend says: "; curl -s localhost:8000/count; echo
echo -n "[app] frontend head: "; curl -s -o /dev/null -w '%{http_code}' localhost:3000/; echo
