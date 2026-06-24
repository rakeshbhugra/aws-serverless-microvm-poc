#!/usr/bin/env bash
# MicroVM entrypoint: start the in-VM Docker daemon, then `docker compose up`.
# NOTE: no `set -e` — we keep the VM alive no matter what so a failed stack can
# still be inspected via `microvm.py logs` or a shell, instead of the VM
# terminating (which is what gave us the opaque 502 last time).
set -uo pipefail

echo "[entrypoint] starting debug-exec server on :9000..."
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

echo "[entrypoint] starting dockerd..."
dockerd >/var/log/dockerd.log 2>&1 &

echo "[entrypoint] waiting for docker daemon..."
for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "[entrypoint] WARNING: dockerd never came up:"; tail -n 50 /var/log/dockerd.log
fi

echo "[entrypoint] docker compose up..."
if docker compose up -d; then
  echo "[entrypoint] compose up OK"
else
  echo "[entrypoint] compose FAILED — leaving VM alive for debugging (see logs)"
fi

echo "[entrypoint] stack status:"
docker compose ps || true

echo "[entrypoint] keeping VM alive"
exec tail -f /dev/null
