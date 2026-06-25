"""Agent stream server (:9100) — POC 4.

Runs `claude -p --output-format stream-json` (or a token-free demo producer)
inside the VM, captures each event line into a **Redis Stream**, and relays the
stream to SSE consumers. The Redis buffer is the point: a consumer can drop and
reconnect with Last-Event-ID (or ?from=<id>) to replay from where it left off —
the stepping stone to POC 5's central/ElastiCache Redis.

Endpoints (reached over the MicroVM's authenticated ingress on :9100):
  POST /run    {"task": "...", "token": "..."}   -> start a real Claude run
               {"demo": true}                    -> start a canned producer (no token)
  GET  /stream [?from=<id>]                       -> SSE of buffered + live events
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import redis

R = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
STREAM = "agent:events"


def emit(obj: dict):
    R.xadd(STREAM, {"data": json.dumps(obj)})


def run_claude(task: str, token: str):
    emit({"type": "start", "task": task})
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=token)
    try:
        proc = subprocess.Popen(
            ["claude", "-p", task, "--output-format", "stream-json", "--verbose",
             "--dangerously-skip-permissions"],
            cwd="/workspace", env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                emit({"type": "claude", "line": line})
        proc.wait()
        emit({"type": "done", "exit": proc.returncode})
    except Exception as e:  # noqa: BLE001
        emit({"type": "done", "exit": -1, "error": str(e)})


def run_demo():
    emit({"type": "start", "task": "demo"})
    for i in range(1, 6):
        time.sleep(0.6)
        emit({"type": "claude", "line": json.dumps({"event": "thinking", "step": i})})
    emit({"type": "done", "exit": 0})


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") != "/run":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}")
        if body.get("demo"):
            threading.Thread(target=run_demo, daemon=True).start()
        elif body.get("token"):
            threading.Thread(target=run_claude, args=(body.get("task", ""), body["token"]), daemon=True).start()
        else:
            self.send_error(400, "task+token (or demo) required")
            return
        self._json({"ok": True, "stream": STREAM})

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") not in ("/stream", ""):
            self.send_error(404)
            return
        cursor = self.headers.get("Last-Event-ID") or (
            parse_qs(urlparse(self.path).query).get("from") or ["0"]
        )[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                resp = R.xread({STREAM: cursor}, block=15000, count=20)
                if not resp:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for _stream, entries in resp:
                    for eid, fields in entries:
                        cursor = eid
                        data = fields.get("data", "")
                        self.wfile.write(f"id: {eid}\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                        try:
                            if json.loads(data).get("type") == "done":
                                return
                        except ValueError:
                            pass
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("agent-server listening on :9100", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
