# MicroVM image for POC 8 — persistent, run-keyed Claude sessions with S3 snapshot/restore.
# Chat-only: no GitHub broker. The Claude credential is fetched VM-side from Secrets Manager
# (POC 5); a run's on-disk state is snapshotted to / restored from S3 (persist.py), driven by
# the MicroVM lifecycle hooks (hooks.py) and driver-triggered endpoints (agent-server.py).
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*
# (tar + gzip are already in the base image, used for snapshot/restore.)

# Python venv with boto3 (Secrets Manager + S3). No redis, no PyJWT/cryptography.
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir boto3

# Claude Code CLI.
RUN npm i -g @anthropic-ai/claude-code

# Workspace (what Claude edits) + the servers. Claude runs as the non-root 'node' user.
COPY workspace/ /workspace/
RUN chown -R node:node /workspace
COPY agent-server.py /agent-server.py
COPY hooks.py /hooks.py
COPY persist.py /persist.py
COPY debug-exec.py /debug-exec.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
# 9000 debug-exec, 9100 agent-server, 8080 lifecycle-hooks (must match hooks.port at build).
EXPOSE 8080 9000 9100
CMD ["/entrypoint.sh"]
