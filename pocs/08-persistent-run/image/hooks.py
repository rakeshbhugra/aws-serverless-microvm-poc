"""MicroVM lifecycle-hooks server — POC 8.

Lambda POSTs to /aws/lambda-microvms/runtime/v1/{run,resume,suspend,terminate} on the
port declared in the image's `hooks.port` config (HOOKS_PORT here). These fire *inside*
the guest, invoked by the Lambda MicroVM runtime — not by our control plane.

  /run       - a fresh VM started. Body {microvmId, runHookPayload}; runHookPayload carries
               {"sid":"<uuid>"}. We restore that run's state from S3, then return 200 —
               traffic only flows after this returns, so the FS is hydrated before the
               first request. We record the active sid for later suspend/terminate flushes.
  /resume    - VM resumed from suspend (state already in RAM/disk). Return 200.
  /suspend   - about to suspend. Flush the active run's state to S3.
  /terminate - about to terminate (incl. the 8h cap). Final flush to S3.

Each runtime hook must complete within 60s (AWS cap), so snapshots are kept lean
(see persist.SNAPSHOT_PATHS). All handlers return 200 even on internal error so a
snapshot hiccup never blocks the lifecycle transition (we log the error instead).
"""

import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import persist

HOOKS_PORT = int(os.environ.get("HOOKS_PORT", "8080"))
BASE = "/aws/lambda-microvms/runtime/v1"
ACTIVE_SID_FILE = "/tmp/active_sid"


def _log(msg: str):
    print(f"[hooks {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _set_active(sid: str):
    try:
        with open(ACTIVE_SID_FILE, "w") as f:
            f.write(sid)
    except OSError as e:
        _log(f"could not persist active sid: {e}")


def _get_active() -> str | None:
    try:
        with open(ACTIVE_SID_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        hook = self.path.rstrip("/").split("/")[-1]
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        try:
            status = self._handle(hook, body)
        except Exception as e:  # noqa: BLE001 — never block a lifecycle transition
            _log(f"/{hook} ERROR: {e}")
            status = 200
        self.send_response(status)
        self.end_headers()

    def _handle(self, hook: str, body: dict) -> int:
        if hook == "ready":
            # Build-time: signal init complete so Lambda snapshots a ready VM. Required
            # whenever any runtime hook is enabled. Wait for the app servers to bind.
            up = _port_up(9100) and _port_up(9000)
            _log(f"/ready servers_up={up}")
            return 200 if up else 503
        if hook == "run":
            sid = None
            payload = body.get("runHookPayload")
            if payload:
                try:
                    sid = json.loads(payload).get("sid")
                except (ValueError, TypeError):
                    sid = None
            if sid:
                _set_active(sid)
                r = persist.restore(sid)
                _log(f"/run sid={sid} restore={r}")
            else:
                _log(f"/run no sid in payload={payload!r}; fresh VM")
        elif hook == "resume":
            _log("/resume (state intact)")
        elif hook in ("suspend", "terminate"):
            sid = _get_active()
            if sid:
                r = persist.snapshot(sid)
                _log(f"/{hook} sid={sid} snapshot={r}")
            else:
                _log(f"/{hook} no active sid; nothing to flush")
        else:
            _log(f"unknown hook /{hook}")
        return 200

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"hooks listening on :{HOOKS_PORT} ({BASE}/*)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", HOOKS_PORT), Handler).serve_forever()
