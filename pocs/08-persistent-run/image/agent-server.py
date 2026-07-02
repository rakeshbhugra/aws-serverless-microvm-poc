"""Agent stream server (:9100) — POC 8 (persistent, run-keyed sessions).

Runs `claude -p --output-format stream-json` inside the VM and relays each event over
SSE (in-process queue, no redis). Continuity is keyed by a caller-supplied UUID `sid`:

  - first turn of a run  -> `claude --session-id <sid>` (creates the transcript)
  - later turns          -> `claude --resume <sid>`    (continues with full context)

The transcript lives at /home/node/.claude/projects/-workspace/<sid>.jsonl (cwd /workspace
=> project slug "-workspace"); we pick --resume vs --session-id by whether that file exists.

Durability is external: the run's disk state is snapshotted to S3 (see persist.py). This
server exposes driver-triggered POST /snapshot and POST /restore; hooks.py triggers the
same helpers automatically on the MicroVM lifecycle (/run,/suspend,/terminate).

Endpoints (over the MicroVM's authenticated ingress on :9100):
  POST /run       {"task":"...", "sid":"<uuid>"}   -> start a turn; returns {"ok":true,"run":<id>}
                  {"demo": true}                    -> canned producer (no creds)
  GET  /stream?run=<id>                             -> SSE of that run's events
  POST /snapshot  {"sid":"<uuid>"}                  -> tar the run's paths -> S3
  POST /restore   {"sid":"<uuid>"}                  -> download from S3 -> untar into place
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3

import persist

SECRET_NAME = os.environ.get("SECRET_NAME", "microvm/claude-api-key")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
PROJECT_DIR = "/home/node/.claude/projects/-workspace"  # cwd /workspace => slug "-workspace"

# In-process run registry (SSE). Each turn gets a Queue drained by /stream.
RUNS: dict[str, queue.Queue] = {}
_LOCK = threading.Lock()
_COUNTER = 0


def _new_run() -> str:
    global _COUNTER
    with _LOCK:
        _COUNTER += 1
        rid = str(_COUNTER)
        RUNS[rid] = queue.Queue()
    return rid


def emit(rid: str, obj: dict):
    q = RUNS.get(rid)
    if q is not None:
        q.put(obj)


def fetch_credential() -> str:
    """Fetch the Anthropic credential from Secrets Manager using the VM's runtime role."""
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    return sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"].strip()


def credential_env(cred: str) -> dict:
    var = "CLAUDE_CODE_OAUTH_TOKEN" if cred.startswith("sk-ant-oat") else "ANTHROPIC_API_KEY"
    return {var: cred}


def _valid_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, TypeError):
        return False


def run_claude(rid: str, task: str, sid: str):
    emit(rid, {"type": "start", "task": task, "sid": sid})
    try:
        cred = fetch_credential()
    except Exception as e:  # noqa: BLE001
        emit(rid, {"type": "done", "exit": -1, "error": f"claude secret fetch failed: {e}"})
        return
    transcript = f"{PROJECT_DIR}/{sid}.jsonl"
    resuming = os.path.exists(transcript)
    session_flag = ["--resume", sid] if resuming else ["--session-id", sid]
    emit(rid, {"type": "claude", "line": f"[agent-server] {'resuming' if resuming else 'new'} session {sid}"})
    env = dict(os.environ, HOME="/home/node", **credential_env(cred))
    try:
        proc = subprocess.Popen(
            ["claude", "-p", task, *session_flag, "--output-format", "stream-json",
             "--verbose", "--dangerously-skip-permissions"],
            cwd="/workspace", env=env, user="node", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                emit(rid, {"type": "claude", "line": line})
        proc.wait()
        emit(rid, {"type": "done", "exit": proc.returncode})
    except Exception as e:  # noqa: BLE001
        emit(rid, {"type": "done", "exit": -1, "error": str(e)})


def run_demo(rid: str):
    emit(rid, {"type": "start", "task": "demo"})
    for i in range(1, 6):
        time.sleep(0.6)
        emit(rid, {"type": "claude", "line": json.dumps({"event": "thinking", "step": i})})
    emit(rid, {"type": "done", "exit": 0})


class Handler(BaseHTTPRequestHandler):
    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or "{}")

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/run":
            body = self._read_body()
            if body.get("demo"):
                rid = _new_run()
                threading.Thread(target=run_demo, args=(rid,), daemon=True).start()
            elif body.get("task") and body.get("sid"):
                if not _valid_uuid(body["sid"]):
                    self.send_error(400, "sid must be a UUID")
                    return
                rid = _new_run()
                threading.Thread(target=run_claude,
                                 args=(rid, body["task"], body["sid"]), daemon=True).start()
            else:
                self.send_error(400, "task+sid (or demo) required")
                return
            self._json({"ok": True, "run": rid})
        elif path in ("/snapshot", "/restore"):
            body = self._read_body()
            sid = body.get("sid")
            if not _valid_uuid(sid):
                self.send_error(400, "sid must be a UUID")
                return
            try:
                fn = persist.snapshot if path == "/snapshot" else persist.restore
                self._json(fn(sid))
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, code=500)
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") not in ("/stream", ""):
            self.send_error(404)
            return
        q = parse_qs(urlparse(self.path).query)
        rid = (q.get("run") or [""])[0]
        runq = RUNS.get(rid)
        if not rid or runq is None:
            self.send_error(404, "unknown run")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    obj = runq.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                if obj.get("type") == "done":
                    RUNS.pop(rid, None)
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("agent-server listening on :9100", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
