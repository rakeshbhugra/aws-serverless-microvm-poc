"""Hello-world app for the MicroVM lifecycle POC (roadmap track 1).

A minimal HTTP server on port 8080 (the MicroVM default ingress port), stdlib
only — no deps to install. The response is deliberately chosen to TEACH two
snapshot behaviours:

    booted_at  — stamped ONCE at process start, which happens at image BUILD
                 time (before the snapshot). Every MicroVM launched from this
                 image shows the SAME value -> demonstrates §05 "shared initial
                 state". Anything unique must be generated after /run, not here.

    hits       — an in-memory counter. It survives suspend/resume because the
                 snapshot captures RAM -> demonstrates stateful resumability.
                 (Resets only when you terminate and run a fresh VM.)
"""

import json
import os
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

booted_at = datetime.now(timezone.utc).isoformat()  # build-time -> shared across VMs
hits = 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global hits
        hits += 1
        body = json.dumps(
            {
                "status": "ok",
                "path": self.path,
                "hits": hits,           # proves state survives suspend/resume
                "booted_at": booted_at,  # identical across VMs (snapshot gotcha)
                "host": socket.gethostname(),
            },
            indent=2,
        ) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):  # quiet default logging
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"hello-world listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
