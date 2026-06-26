# MicroVM image for POC 4 — agent streaming. The Redis stream buffer is now an
# external ElastiCache cluster (reached over a VPC egress connector), so no local
# redis-server is installed/run. Just Node (Claude CLI) + Python + the redis client.
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates curl

# Python venv with the redis client (for agent-server.py talking to ElastiCache).
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir redis

# Claude Code CLI.
RUN npm i -g @anthropic-ai/claude-code

# Workspace (what Claude edits) + the servers. Claude runs as the image's
# existing non-root 'node' user (uid 1000) — it refuses --dangerously-skip-
# permissions as root — so the workspace must be node-writable.
COPY workspace/ /workspace/
RUN chown -R node:node /workspace
COPY agent-server.py /agent-server.py
COPY debug-exec.py /debug-exec.py
COPY redis_host /redis_host
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
EXPOSE 9000 9100
CMD ["/entrypoint.sh"]
