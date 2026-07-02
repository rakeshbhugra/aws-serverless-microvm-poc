#!/usr/bin/env python3
"""Drive the persistent-run MicroVM (roadmap track 8 — a Claude/Codex run that survives
across MicroVM lifecycles via S3 snapshots).

A run is keyed by a caller-supplied UUID (SID). It is both Claude's session id
(`--session-id`/`--resume`, deterministic) and the S3 key prefix for the run's snapshot.
The MicroVM fetches the Claude credential from Secrets Manager (POC 5), and snapshots the
run's on-disk state to s3://<bucket>/microvm-runs/<sid>/ — restored into a fresh VM on the
`/run` lifecycle hook, flushed on `/suspend` and `/terminate`. So an agent can be resumed
hours/days later, in a brand-new VM, with full context (the 8h VM lifetime is a hard cap;
durability lives in S3, not the VM).

    uv run python microvm.py check / prereqs / package / build / wait-image
    SID=$(uuidgen) uv run python microvm.py run / wait / token
    SID=... uv run python microvm.py agent "task"    # one-shot turn on the run
    SID=... uv run python microvm.py chat            # interactive REPL on the run
    uv run python microvm.py snapshot / restore      # driver-triggered S3 flush/rehydrate
    uv run python microvm.py suspend / resume / terminate / clean

Chat-only: no GitHub. See README for the run-keyed persistence model and verification.
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).parent
IMAGE_DIR = HERE / "image"
WORKSPACE_DIR = HERE / "workspace"
STATE_FILE = HERE / ".state.json"

REGION = os.environ.get("REGION", "us-east-1")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "ryu-persistent-run")
ROLE_NAME = os.environ.get("ROLE_NAME", "MicrovmBuildRole")
RUNTIME_ROLE_NAME = os.environ.get("RUNTIME_ROLE_NAME", "QhiveMicrovmRuntimeRole")
SECRET_NAME = os.environ.get("SECRET_NAME", "microvm/claude-api-key")
ZIP_KEY = "persistent-run.zip"
MEMORY_MIB = int(os.environ.get("MEMORY_MIB", "2048"))

# Snapshot config (baked into the image env at build; the VM reads its own bucket/prefix).
SNAPSHOT_PREFIX = os.environ.get("SNAPSHOT_PREFIX", "microvm-runs")
SNAPSHOT_PATHS = os.environ.get("SNAPSHOT_PATHS", "/home/node/.claude/projects")
HOOKS_PORT = int(os.environ.get("HOOKS_PORT", "8080"))

BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
IDLE_POLICY = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 3600}


def state_load() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def state_save(**kw) -> dict:
    s = state_load()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    return s


def need(key: str):
    v = state_load().get(key)
    if not v:
        sys.exit(f"missing '{key}' — run earlier steps first")
    return v


def resolve_sid() -> str:
    """The run id / Claude session id / S3 key. From $SID, else the last one in state, else
    a fresh uuid4 (saved + announced). Deterministic: pass the same SID to address a run."""
    s = os.environ.get("SID") or state_load().get("sid")
    if not s:
        s = str(uuid.uuid4())
        print(f"==> new run sid {s} (reuse it with:  SID={s} …)")
    state_save(sid=s)
    return s


def mv():
    return boto3.client("lambda-microvms", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return os.environ.get("BUCKET", f"ryu-microvm-poc-{account_id()}")


def image_arn() -> str:
    return state_load().get("image_arn") or f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{IMAGE_NAME}"


def _hdr(port: str) -> dict:
    return {"X-aws-proxy-auth": need("token"), "X-aws-proxy-port": port}


# ---- lifecycle --------------------------------------------------------------
def cmd_check(_):
    boto3.client("sts").get_caller_identity()
    mv()
    sm = boto3.client("secretsmanager", region_name=REGION)
    try:
        sm.describe_secret(SecretId=SECRET_NAME)
        claude = "ok"
    except ClientError as e:
        claude = f"MISSING ({e.response.get('Error', {}).get('Code')})"
    print(f"  OK region={REGION} image={IMAGE_NAME} mem={MEMORY_MIB}MiB runtime_role={RUNTIME_ROLE_NAME}\n"
          f"     claude_secret={SECRET_NAME} [{claude}]\n"
          f"     snapshots=s3://{bucket_name()}/{SNAPSHOT_PREFIX}/<sid>/  (runtime role needs s3 Get/Put here)\n"
          f"     snapshot_paths={SNAPSHOT_PATHS}  hooks_port={HOOKS_PORT}\n"
          f"     sid={os.environ.get('SID') or state_load().get('sid') or '(none yet — set SID or one is generated on run)'}")


TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
         "Principal": {"Service": "lambda.amazonaws.com"}, "Action": ["sts:AssumeRole", "sts:TagSession"]}]}


def perm_policy(b):
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{b}/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": "arn:aws:logs:*:*:*"}]}


def cmd_prereqs(_):
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam")
    b = bucket_name()
    try:
        s3.head_bucket(Bucket=b)
    except ClientError:
        kw = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=b, **kw)
    try:
        iam.get_role(RoleName=ROLE_NAME)
    except ClientError:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST))
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="microvm-build", PolicyDocument=json.dumps(perm_policy(b)))
    # Runtime role is operator-provisioned. For POC 8 it must grant secretsmanager:GetSecretValue on
    # the Claude secret AND s3:GetObject/PutObject on s3://<bucket>/microvm-runs/* (for snapshots).
    try:
        runtime_arn = iam.get_role(RoleName=RUNTIME_ROLE_NAME)["Role"]["Arn"]
    except ClientError:
        sys.exit(f"runtime role '{RUNTIME_ROLE_NAME}' not found — create it (trust lambda.amazonaws.com; "
                 f"grant secretsmanager:GetSecretValue on {SECRET_NAME} and s3 Get/Put on "
                 f"arn:aws:s3:::{b}/{SNAPSHOT_PREFIX}/*) or set RUNTIME_ROLE_NAME")
    state_save(bucket=b, role_arn=f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}", runtime_role_arn=runtime_arn)
    print(f"==> bucket + build role ready; runtime role {runtime_arn}\n"
          f"    NOTE: ensure the runtime role has s3:GetObject/PutObject on "
          f"arn:aws:s3:::{b}/{SNAPSHOT_PREFIX}/* (operator-added). Sleeping 10s for IAM.")
    time.sleep(10)


def cmd_package(_):
    import zipfile
    b = state_load().get("bucket") or bucket_name()
    zp = HERE / ZIP_KEY
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(IMAGE_DIR.iterdir()):
            if f.is_file():
                z.write(f, f.name)
        for p in sorted(WORKSPACE_DIR.rglob("*")):
            if p.is_file():
                z.write(p, f"workspace/{p.relative_to(WORKSPACE_DIR)}")
    boto3.client("s3", region_name=REGION).upload_file(str(zp), b, ZIP_KEY)
    print(f"==> uploaded s3://{b}/{ZIP_KEY}")


def cmd_build(_):
    s = state_load()
    b = s.get("bucket") or bucket_name()
    # Bake snapshot config into the image env; declare the lifecycle hooks so Lambda posts
    # /run,/suspend,/terminate to our in-guest hooks server on HOOKS_PORT (≤60s each).
    env_vars = {"SECRET_NAME": SECRET_NAME, "AWS_DEFAULT_REGION": REGION,
                "SNAPSHOT_BUCKET": b, "SNAPSHOT_PREFIX": SNAPSHOT_PREFIX,
                "SNAPSHOT_PATHS": SNAPSHOT_PATHS, "HOOKS_PORT": str(HOOKS_PORT)}
    # /ready (build hook) is REQUIRED whenever any runtime hook is enabled — it signals the
    # app initialized so Lambda snapshots a ready VM.
    hooks = {"port": HOOKS_PORT,
             "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 300},
             "microvmHooks": {
                 "run": "ENABLED", "runTimeoutInSeconds": 60,
                 "suspend": "ENABLED", "suspendTimeoutInSeconds": 60,
                 "terminate": "ENABLED", "terminateTimeoutInSeconds": 60}}
    common = dict(codeArtifact={"uri": f"s3://{b}/{ZIP_KEY}"}, baseImageArn=BASE_IMAGE_ARN,
                  buildRoleArn=s["role_arn"], resources=[{"minimumMemoryInMiB": MEMORY_MIB}],
                  hooks=hooks, environmentVariables=env_vars)
    try:
        resp = mv().create_microvm_image(name=IMAGE_NAME, **common)
        print("==> create-microvm-image (hooks: run/suspend/terminate on :%d)" % HOOKS_PORT)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("ConflictException", "ResourceConflictException") or "exist" in str(e).lower():
            resp = mv().update_microvm_image(imageIdentifier=image_arn(), **common)
            print("==> update-microvm-image")
        else:
            raise
    if resp.get("imageArn"):
        state_save(image_arn=resp["imageArn"])
    state_save(image_version=resp.get("imageVersion"))
    print(f"    building version {resp.get('imageVersion')}; poll: uv run python microvm.py wait-image")


def cmd_wait_image(_):
    want = state_load().get("image_version")
    while True:
        vers = {v.get("imageVersion"): v for v in
                mv().list_microvm_image_versions(imageIdentifier=image_arn()).get("items", [])}
        if want and want in vers:
            st = vers[want].get("state")
            print(f"    version {want} state: {st}")
            if st == "SUCCESSFUL":
                return
            if st == "FAILED":
                sys.exit(f"version {want} build FAILED — CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        else:
            st = mv().get_microvm_image(imageIdentifier=image_arn()).get("state")
            print(f"    image state: {st}")
            if st in ("CREATED", "UPDATED"):
                return
            if st and "FAILED" in st:
                sys.exit(f"build failed — CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        time.sleep(10)


def cmd_run(_):
    # INTERNET_EGRESS + runtime role (Claude secret + S3). runHookPayload carries the run id
    # so the /run hook restores this run's state from S3 before traffic flows.
    sid = resolve_sid()
    resp = mv().run_microvm(imageIdentifier=image_arn(), ingressNetworkConnectors=[INGRESS],
                            egressNetworkConnectors=[EGRESS], idlePolicy=IDLE_POLICY,
                            executionRoleArn=need("runtime_role_arn"),
                            runHookPayload=json.dumps({"sid": sid}))
    state_save(microvm_id=resp["microvmId"], endpoint=resp["endpoint"])
    print(f"==> microvm {resp['microvmId']} (sid {sid})\n    https://{resp['endpoint']}")


def cmd_wait(_):
    mid = need("microvm_id")
    while True:
        st = mv().get_microvm(microvmIdentifier=mid).get("state")
        print(f"    microvm state: {st}")
        if st == "RUNNING":
            return
        if st == "TERMINATED" or (st and "FAILED" in st):
            sys.exit(f"unexpected state: {st}")
        time.sleep(5)


def cmd_token(_):
    mid = need("microvm_id")
    resp = mv().create_microvm_auth_token(microvmIdentifier=mid, expirationInMinutes=60, allowedPorts=[{"allPorts": {}}])
    state_save(token=resp["authToken"]["X-aws-proxy-auth"])
    print("==> token minted (60 min)")


# ---- turns + streaming ------------------------------------------------------
def _post_run(body: dict) -> str:
    endpoint = need("endpoint")
    req = urllib.request.Request(f"https://{endpoint}/run", data=json.dumps(body).encode(),
                                 headers={**_hdr("9100"), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    return str(resp["run"])


def _render_claude(rawline: str, raw: bool) -> str:
    if raw:
        return rawline + "\n"
    try:
        ev = json.loads(rawline)
    except ValueError:
        return f"\n· {rawline}\n" if rawline.startswith("[agent-server]") else ""
    if ev.get("type") == "assistant":
        out = []
        for blk in ev.get("message", {}).get("content", []):
            if blk.get("type") == "text":
                out.append(blk.get("text", ""))
            elif blk.get("type") == "tool_use":
                out.append(f"\n[tool: {blk.get('name')}]\n")
        return "".join(out)
    return ""


def _render_stream(run: str, raw: bool = False):
    """Stream one turn's events, rendering Claude's text. Reconnects if the SSE GET resets
    (Zscaler) — the in-proc queue is drain-once, so reconnect continues without duplication."""
    endpoint = need("endpoint")
    prefixed = False
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"https://{endpoint}/stream?run={run}", headers=_hdr("9100"))
            with urllib.request.urlopen(req, timeout=600) as r:
                for rawb in r:
                    line = rawb.decode(errors="replace").rstrip("\n")
                    if not line.startswith("data:"):
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    t = obj.get("type")
                    if t == "claude":
                        text = _render_claude(obj.get("line", ""), raw)
                        if text:
                            if not prefixed:
                                print("claude> ", end="")
                                prefixed = True
                            print(text, end="", flush=True)
                    elif t == "done":
                        print()
                        if obj.get("error"):
                            print(f"[error: {obj['error']}]")
                        return
            return  # stream closed cleanly without 'done'
        except (urllib.error.URLError, ConnectionError) as e:
            print(f"\n[stream reset, reconnecting… {e}]")
            continue
    print("[gave up after repeated stream resets]")


def cmd_demo_stream(_):
    _render_stream(_post_run({"demo": True}))


def cmd_agent(args):
    if not (args and args.rest):
        sys.exit("usage: agent \"<task>\"  (set SID=<uuid> to address a run)")
    sid = resolve_sid()
    task = " ".join(args.rest)
    print(f"  (sid {sid})")
    _render_stream(_post_run({"task": task, "sid": sid}), raw=False)


def cmd_chat(args):
    sid = resolve_sid()
    raw = bool(args and "--raw" in args.rest)
    print(f"chat on run {sid}  —  Ctrl-D or /exit to quit, /snapshot to flush, /session to show id")
    while True:
        try:
            msg = input("you> ").strip()
        except EOFError:
            print()
            break
        if not msg:
            continue
        if msg in ("/exit", "/quit"):
            break
        if msg == "/session":
            print(f"  {sid}")
            continue
        if msg == "/snapshot":
            _snap("/snapshot", sid)
            continue
        _render_stream(_post_run({"task": msg, "sid": sid}), raw=raw)


def _snap(path: str, sid: str):
    endpoint = need("endpoint")
    req = urllib.request.Request(f"https://{endpoint}{path}", data=json.dumps({"sid": sid}).encode(),
                                 headers={**_hdr("9100"), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        print(f"  {path}: {json.loads(r.read())}")


def cmd_snapshot(_):
    _snap("/snapshot", resolve_sid())


def cmd_restore(_):
    _snap("/restore", resolve_sid())


def cmd_shell(args):
    endpoint = need("endpoint")
    cmd = " ".join(args.rest) if args and args.rest else "echo specify a command"
    req = urllib.request.Request(f"https://{endpoint}/", data=cmd.encode(), headers=_hdr("9000"), method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    print(out.get("stdout", ""), end="")
    print(out.get("stderr", ""), end="")


def cmd_logs(_):
    logs = boto3.client("logs", region_name=REGION)
    group = f"/aws/lambda-microvms/{IMAGE_NAME}"
    streams = logs.describe_log_streams(logGroupName=group, orderBy="LastEventTime", descending=True, limit=1).get("logStreams", [])
    if not streams:
        sys.exit(f"no log streams in {group} yet")
    for e in logs.get_log_events(logGroupName=group, logStreamName=streams[0]["logStreamName"], limit=200, startFromHead=False)["events"]:
        print(e["message"].rstrip())


def _simple(method, label):
    def fn(_):
        mid = need("microvm_id")
        getattr(mv(), method)(microvmIdentifier=mid)
        print(f"==> {label}: {mid}")
    return fn


def cmd_clean(_):
    s = state_load()
    if s.get("microvm_id"):
        try:
            mv().terminate_microvm(microvmIdentifier=s["microvm_id"])
            print("    terminated (S3 snapshot for the run is kept)")
        except ClientError as e:
            print(f"    (terminate skipped: {e})")
    try:
        mv().delete_microvm_image(imageIdentifier=image_arn())
        print("    image deleted")
    except ClientError as e:
        print(f"    (image delete skipped: {e})")
    for k in ("microvm_id", "endpoint", "image_arn", "image_version", "token"):
        s.pop(k, None)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    print("==> cleaned (sid kept in state; S3 run snapshots untouched)")


COMMANDS = {
    "check": cmd_check, "prereqs": cmd_prereqs,
    "package": cmd_package, "build": cmd_build, "wait-image": cmd_wait_image,
    "run": cmd_run, "wait": cmd_wait, "token": cmd_token,
    "demo-stream": cmd_demo_stream, "agent": cmd_agent, "chat": cmd_chat,
    "snapshot": cmd_snapshot, "restore": cmd_restore, "shell": cmd_shell, "logs": cmd_logs,
    "suspend": _simple("suspend_microvm", "suspended"), "resume": _simple("resume_microvm", "resumed"),
    "terminate": _simple("terminate_microvm", "terminated"), "clean": cmd_clean,
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    p.add_argument("rest", nargs="*")
    args = p.parse_args()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
