# MicroVM image for the agent sandbox: a flattened Debian toolchain (no Docker).
# Everything the agent might need is baked: Node (Claude CLI + Playwright),
# Python (backend), Redis, and a headless Chromium with its system deps.
FROM node:22-bookworm

# System deps for the app + headless browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv redis-server ca-certificates curl

# Python venv with the backend's deps (bookworm python is externally-managed).
RUN python3 -m venv /opt/venv
COPY workspace/backend/requirements.txt /tmp/req.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/req.txt

# Node tooling: Claude Code + Playwright (global) + Chromium with its apt deps.
# NODE_PATH lets a CommonJS script resolve the global playwright from any cwd.
ENV NODE_PATH=/usr/local/lib/node_modules
RUN npm i -g @anthropic-ai/claude-code playwright \
 && playwright install --with-deps chromium

# The app workspace (what the agent edits) + helper scripts.
COPY workspace/ /workspace/
COPY debug-exec.py /debug-exec.py
COPY entrypoint.sh /entrypoint.sh
COPY app-up.sh /opt/app-up.sh
COPY shot.cjs /opt/shot.cjs
RUN chmod +x /entrypoint.sh /opt/app-up.sh && mkdir -p /workspace/screenshots

WORKDIR /workspace
EXPOSE 3000 8000 9000
CMD ["/entrypoint.sh"]
