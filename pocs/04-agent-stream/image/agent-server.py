"""Agent stream server (:9100) — POC 4.

Runs `claude -p --output-format stream-json` (or a token-free demo producer)
inside the VM, captures each event line into a **per-run Redis Stream**, and
relays it over **SSE**. One stream per run (`agent:events:<runId>`) so runs never
replay each other; the Redis buffer still lets a consumer reconnect and replay a
given run via Last-Event-ID / ?from=<id> — the stepping stone to POC 5's
central/ElastiCache Redis.

Endpoints (over the MicroVM's authenticated ingress on :9100):
  POST /run    {"task": "...", "token": "..."}   -> start a real Claude run
               {"demo": true}                    -> start a canned producer (no token)
               returns {"ok": true, "run": <id>, "stream": "agent:events:<id>"}
  GET  /stream?run=<id>[&from=<event-id>]         -> SSE of that run's events
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


def emit(stream: str, obj: dict):
    R.xadd(stream, {"data": json.dumps(obj)})


def run_claude(stream: str, task: str, token: str):
    emit(stream, {"type": "start", "task": task})
    # Run as the non-root 'node' user (claude blocks --dangerously-skip-permissions as root).
    env = dict(os.environ, HOME="/home/node", CLAUDE_CODE_OAUTH_TOKEN=token)
    try:
        proc = subprocess.Popen(
            ["claude", "-p", task, "--output-format", "stream-json", "--verbose",
             "--dangerously-skip-permissions"],
            cwd="/workspace", env=env, user="node", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                emit(stream, {"type": "claude", "line": line})
        proc.wait()
        emit(stream, {"type": "done", "exit": proc.returncode})
    except Exception as e:  # noqa: BLE001
        emit(stream, {"type": "done", "exit": -1, "error": str(e)})


def run_demo(stream: str):
    emit(stream, {"type": "start", "task": "demo"})
    for i in range(1, 6):
        time.sleep(0.6)
        emit(stream, {"type": "claude", "line": json.dumps({"event": "thinking", "step": i})})
    emit(stream, {"type": "done", "exit": 0})


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") != "/run":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}")
        rid = R.incr("agent:runs")
        stream = f"agent:events:{rid}"
        if body.get("demo"):
            threading.Thread(target=run_demo, args=(stream,), daemon=True).start()
        elif body.get("token"):
            threading.Thread(target=run_claude, args=(stream, body.get("task", ""), body["token"]), daemon=True).start()
        else:
            self.send_error(400, "task+token (or demo) required")
            return
        self._json({"ok": True, "run": rid, "stream": stream})

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") not in ("/stream", ""):
            self.send_error(404)
            return
        q = parse_qs(urlparse(self.path).query)
        rid = (q.get("run") or [""])[0]
        if not rid:
            self.send_error(400, "run required")
            return
        stream = f"agent:events:{rid}"
        cursor = self.headers.get("Last-Event-ID") or (q.get("from") or ["0"])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    resp = R.xread({stream: cursor}, block=15000, count=20)
                except redis.exceptions.TimeoutError:
                    resp = None  # no events within the block window — send a keepalive
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
