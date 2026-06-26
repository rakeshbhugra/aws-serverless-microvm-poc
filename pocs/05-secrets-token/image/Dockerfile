# MicroVM image for POC 5 — agent streaming with the Claude credential fetched
# from Secrets Manager inside the VM. Lighter than POC 3 (no Playwright/Chromium):
# just Node (Claude CLI), Python, and Redis (the stream buffer).
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv redis-server ca-certificates curl

# Python venv with redis (stream buffer) + boto3 (Secrets Manager fetch in agent-server.py).
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir redis boto3

# Claude Code CLI.
RUN npm i -g @anthropic-ai/claude-code

# Workspace (what Claude edits) + the servers. Claude runs as the image's
# existing non-root 'node' user (uid 1000) — it refuses --dangerously-skip-
# permissions as root — so the workspace must be node-writable.
COPY workspace/ /workspace/
RUN chown -R node:node /workspace
COPY agent-server.py /agent-server.py
COPY debug-exec.py /debug-exec.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
EXPOSE 9000 9100
CMD ["/entrypoint.sh"]
