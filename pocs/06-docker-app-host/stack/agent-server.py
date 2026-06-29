"""Agent stream server (:9100) — POC 06.

Runs `claude -p --output-format stream-json` inside the VM, buffers each event
into a per-run Redis Stream, and relays it over SSE. Credential is fetched from
Secrets Manager via the VM's runtime role (POC 5) — never sent in the request.

M1 uses an in-VM Redis (REDIS_HOST=127.0.0.1). M4 points REDIS_HOST at a central
ElastiCache so a control plane can read every VM's stream.

Endpoints (over the authenticated ingress on :9100):
  POST /run    {"task": "..."}   -> start a Claude run; returns {"ok",run,stream}
  GET  /stream?run=<id>[&from=<event-id>]  -> SSE of that run's events
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3
import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
SECRET_NAME = os.environ.get("SECRET_NAME", "microvm/claude-api-key")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
AGENT_USER = os.environ.get("AGENT_USER", "agent")
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
MICROVM_ID = os.environ.get("MICROVM_ID", "local")

R = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True, socket_connect_timeout=10)


def emit(stream: str, obj: dict):
    R.xadd(stream, {"data": json.dumps(obj)})


def fetch_credential() -> str:
    """Read the Claude credential from Secrets Manager using the VM's runtime role."""
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    return sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"].strip()


def credential_env(cred: str) -> dict:
    """OAuth tokens (sk-ant-oat) -> CLAUDE_CODE_OAUTH_TOKEN; API keys -> ANTHROPIC_API_KEY."""
    var = "CLAUDE_CODE_OAUTH_TOKEN" if cred.startswith("sk-ant-oat") else "ANTHROPIC_API_KEY"
    return {var: cred}


def run_claude(stream: str, task: str):
    emit(stream, {"type": "start", "task": task})
    try:
        cred = fetch_credential()
    except Exception as e:  # noqa: BLE001
        emit(stream, {"type": "done", "exit": -1, "error": f"secret fetch failed: {e}"})
        return
    env = dict(os.environ, HOME=f"/home/{AGENT_USER}", **credential_env(cred))
    try:
        proc = subprocess.Popen(
            ["claude", "-p", task, "--output-format", "stream-json", "--verbose",
             "--dangerously-skip-permissions"],
            cwd=WORKSPACE, env=env, user=AGENT_USER, stdout=subprocess.PIPE,
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


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") != "/run":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}")
        task = body.get("task")
        if not task:
            self.send_error(400, "task required")
            return
        rid = R.incr("agent:runs")
        # Per-VM keyspace so a central reader (M4) can distinguish VMs.
        stream = f"agent:events:{MICROVM_ID}:{rid}"
        R.sadd("agent:vms", MICROVM_ID)
        threading.Thread(target=run_claude, args=(stream, task), daemon=True).start()
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
        stream = f"agent:events:{MICROVM_ID}:{rid}"
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
                    resp = None
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
    print(f"agent-server :9100 (redis={REDIS_HOST} secret={SECRET_NAME} vm={MICROVM_ID})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
