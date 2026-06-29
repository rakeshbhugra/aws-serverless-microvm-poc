# The MicroVM image — runs Docker INSIDE the VM (POC 06).
#
# M0 (build gate): just Docker engine + compose + buildx, booting dockerd and
# idling so we can prove `docker build` with network RUN steps works in-VM.
# Requires additionalOsCapabilities=["ALL"] (the driver passes it) so dockerd
# can create namespaces / mount overlay2 / set up bridge networking.
#
# Lambda runs THIS Dockerfile from the zip root; it COPYs the rest alongside it.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# Docker engine + compose v2 plugin + buildx (for build-gate approach (c)/(d)).
# python3 is for the debug-exec server.
ARG COMPOSE_VERSION=v2.32.4
ARG BUILDX_VERSION=v0.19.3
RUN dnf install -y docker tar gzip python3 && dnf clean all \
 && mkdir -p /usr/libexec/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64" \
      -o /usr/libexec/docker/cli-plugins/docker-compose \
 && curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-arm64" \
      -o /usr/libexec/docker/cli-plugins/docker-buildx \
 && chmod +x /usr/libexec/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-buildx

# Point Docker's containers at the VM's stub resolver (127.0.0.2). Public
# resolvers are blocked for nested containers; this is the only path that works
# for `docker run`. The build gate tests whether `docker build` can use it too.
RUN mkdir -p /etc/docker
COPY daemon.json /etc/docker/daemon.json

# Node + Claude Code CLI (POC 5), a Python venv (redis + boto3 for agent-server),
# and a non-root 'agent' user with Docker access (claude refuses
# --dangerously-skip-permissions as root; the build/compose run as this user).
RUN dnf install -y nodejs npm && dnf clean all \
 && npm i -g @anthropic-ai/claude-code \
 && python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir redis boto3 \
 && useradd -m -s /bin/bash agent && usermod -aG docker agent \
 && mkdir -p /workspace && chown -R agent:agent /workspace

ENV SECRET_NAME=microvm/claude-api-key AWS_DEFAULT_REGION=us-east-1 \
    REDIS_HOST=127.0.0.1 WORKSPACE=/workspace AGENT_USER=agent

WORKDIR /stack
COPY buildgate ./buildgate
COPY agent-server.py /agent-server.py
COPY entrypoint.sh /entrypoint.sh
COPY debug-exec.py /debug-exec.py
RUN chmod +x /entrypoint.sh

EXPOSE 8080 9000 9100
CMD ["/entrypoint.sh"]
