# Container base image for the application layer. MicroVMs are ARM64-only; the
# AWS-managed base ships a snapshot-safe OpenSSL (openssl-snapsafe-libs) — use it
# even for no-crypto apps to stay on the supported path. The MicroVM *OS* base
# (Amazon Linux 2023) is passed separately as --base-image-arn at build time.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# al2023-minimal ships python3; if your base lacks it, install here.
RUN dnf install -y python3 && dnf clean all

WORKDIR /app
COPY app.py .

# Port the app listens on; default MicroVM ingress routes here.
EXPOSE 8080

# CMD starts the app. Lambda runs this, waits for readiness, then snapshots the
# running process — so the server is already listening when a VM resumes.
CMD ["python3", "app.py"]
