"""Debug-exec server on :9000 — POST a shell command, run it on the VM host,
return {exit, stdout, stderr}. Backs `microvm.py shell` / `pull`. DEBUG ONLY."""

import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cmd = self.rfile.read(n).decode() if n else ""
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True)
        body = json.dumps({"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("debug-exec listening on :9000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
