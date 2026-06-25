#!/usr/bin/env bash
# The VM starts idle — it does NOT auto-run the app. We drive everything via the
# debug-exec server (app-up, shot, agent). Keeps the VM alive no matter what.
set -uo pipefail

echo "[entrypoint] starting debug-exec on :9000"
python3 /debug-exec.py >/var/log/debug-exec.log 2>&1 &

mkdir -p /workspace/screenshots
echo "[entrypoint] ready; idle (drive via microvm.py shell/app-up/shot/agent)"
exec tail -f /dev/null
