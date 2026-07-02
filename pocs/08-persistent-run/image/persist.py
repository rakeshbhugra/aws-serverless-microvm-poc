"""Shared S3 snapshot/restore for a run's on-disk state — POC 8.

A "run" is keyed by a caller-supplied UUID (SID). Its filesystem state (Claude's
session transcript + memory + todos, and optionally the workspace) is tarred and
stored at s3://$SNAPSHOT_BUCKET/$SNAPSHOT_PREFIX/<sid>/state.tgz. A fresh MicroVM
restores it before serving traffic, so the agent continues across VM lifecycles.

Imported by both agent-server.py (driver-triggered /snapshot,/restore) and hooks.py
(lifecycle-hook-triggered). Uses the VM's runtime-role AWS creds (boto3 default chain).

Kept fast: the runtime lifecycle hooks (/run,/suspend,/terminate) cap at 60s, so the
default SNAPSHOT_PATHS is just Claude's small project dir; /workspace is opt-in.
"""

import json
import os
import subprocess
import time

import boto3

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SNAPSHOT_BUCKET = os.environ.get("SNAPSHOT_BUCKET", "")
SNAPSHOT_PREFIX = os.environ.get("SNAPSHOT_PREFIX", "microvm-runs").strip("/")
# Comma-separated absolute paths to persist. Default: only Claude's project dir (small,
# fits the 60s hook budget). Add /workspace via env if you need the working files too.
SNAPSHOT_PATHS = [p.strip() for p in
                  os.environ.get("SNAPSHOT_PATHS", "/home/node/.claude/projects").split(",")
                  if p.strip()]
SNAPSHOT_OWNER = os.environ.get("SNAPSHOT_OWNER", "node:node")
# Opt-in size guard (MiB): refuse to snapshot if the tar would exceed this (0 = no guard).
SNAPSHOT_MAX_MIB = int(os.environ.get("SNAPSHOT_MAX_MIB", "0"))


def _s3():
    return boto3.client("s3", region_name=AWS_REGION)


def _key(sid: str) -> str:
    return f"{SNAPSHOT_PREFIX}/{sid}/state.tgz"


def _meta_key(sid: str) -> str:
    return f"{SNAPSHOT_PREFIX}/{sid}/meta.json"


def _existing_relpaths() -> list[str]:
    """SNAPSHOT_PATHS that actually exist, expressed relative to '/' so the archive can
    be extracted with `tar -C /` back to the same absolute locations."""
    rels = []
    for p in SNAPSHOT_PATHS:
        if os.path.exists(p):
            rels.append(p.lstrip("/"))
    return rels


def snapshot(sid: str) -> dict:
    """tar the run's paths and upload to S3. Returns a summary dict."""
    if not SNAPSHOT_BUCKET:
        raise RuntimeError("SNAPSHOT_BUCKET not set")
    rels = _existing_relpaths()
    if not rels:
        return {"ok": True, "skipped": "no snapshot paths exist yet", "sid": sid}
    tgz = f"/tmp/state-{sid}.tgz"
    # -C / + relative paths so restore lands at the same absolute locations.
    subprocess.run(["tar", "czf", tgz, "-C", "/", *rels], check=True)
    size = os.path.getsize(tgz)
    if SNAPSHOT_MAX_MIB and size > SNAPSHOT_MAX_MIB * 1024 * 1024:
        os.remove(tgz)
        raise RuntimeError(f"snapshot {size} bytes exceeds SNAPSHOT_MAX_MIB={SNAPSHOT_MAX_MIB}")
    s3 = _s3()
    s3.upload_file(tgz, SNAPSHOT_BUCKET, _key(sid))
    meta = {"sid": sid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "paths": SNAPSHOT_PATHS, "bytes": size}
    s3.put_object(Bucket=SNAPSHOT_BUCKET, Key=_meta_key(sid),
                  Body=json.dumps(meta).encode(), ContentType="application/json")
    os.remove(tgz)
    return {"ok": True, "sid": sid, "bytes": size, "key": _key(sid), **{"paths": rels}}


def restore(sid: str) -> dict:
    """Download the run's snapshot (if any) and untar into place. No-op on miss."""
    if not SNAPSHOT_BUCKET:
        raise RuntimeError("SNAPSHOT_BUCKET not set")
    s3 = _s3()
    tgz = f"/tmp/restore-{sid}.tgz"
    try:
        s3.download_file(SNAPSHOT_BUCKET, _key(sid), tgz)
    except Exception as e:  # noqa: BLE001 — NoSuchKey / 404 => fresh run
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        return {"ok": True, "restored": False, "reason": code, "sid": sid}
    subprocess.run(["tar", "xzf", tgz, "-C", "/"], check=True)
    # Restored files must be owned by the non-root 'node' user that claude runs as.
    for p in SNAPSHOT_PATHS:
        if os.path.exists(p):
            subprocess.run(["chown", "-R", SNAPSHOT_OWNER, p], check=False)
    size = os.path.getsize(tgz)
    os.remove(tgz)
    return {"ok": True, "restored": True, "sid": sid, "bytes": size}
