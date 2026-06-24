# The MicroVM image — runs a docker-compose stack INSIDE the VM.
#
# This is the experimental bit: a full Docker daemon running in the MicroVM.
# Requires the image to be built with additionalOsCapabilities=["ALL"] (the
# driver passes it) so dockerd can create namespaces / mount overlay2 / set up
# bridge networking — all within the VM's isolation boundary.
#
# Lambda runs THIS Dockerfile from the zip root; it COPYs the compose project
# (docker-compose.yml, backend/, entrypoint.sh) alongside it.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# Docker engine + the compose v2 plugin (AL2023's `docker` package doesn't bundle
# it). python3 is for the debug-exec server. No buildx: we don't `docker build`
# in-VM (buildkit can't use the 127.0.0.2 resolver — see README), so the stack
# runs stock images and installs deps at container start instead.
ARG COMPOSE_VERSION=v2.32.4
RUN dnf install -y docker tar gzip python3 && dnf clean all \
 && mkdir -p /usr/libexec/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64" \
      -o /usr/libexec/docker/cli-plugins/docker-compose \
 && chmod +x /usr/libexec/docker/cli-plugins/docker-compose

# Fix A: point Docker's containers at real DNS servers. The VM's embedded
# resolver (127.0.0.11 NAT) doesn't resolve for nested containers, which breaks
# `pip install` and any in-container network call. dockerd applies this to every
# container it spawns (build and run).
RUN mkdir -p /etc/docker
COPY daemon.json /etc/docker/daemon.json

WORKDIR /stack
COPY docker-compose.yml .
COPY backend ./backend
COPY entrypoint.sh /entrypoint.sh
COPY debug-exec.py /debug-exec.py
RUN chmod +x /entrypoint.sh

EXPOSE 8080 9000
CMD ["/entrypoint.sh"]
