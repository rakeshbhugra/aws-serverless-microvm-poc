#!/usr/bin/env python3
"""Drive the agent-sandbox MicroVM (roadmap track 3).

A flattened Debian image (Node + Python + Redis + Playwright/Chromium + Claude
CLI) with the counter app baked at /workspace. The VM starts idle; we drive it:

    uv run python microvm.py check / prereqs / package / build / wait-image
    uv run python microvm.py run / wait / token
    uv run python microvm.py app-up                 # start redis+backend+frontend
    uv run python microvm.py shot                    # playwright screenshot -> /workspace/screenshots/button.png
    uv run python microvm.py pull /workspace/screenshots/button.png ./button.png
    uv run python microvm.py shell "ls /workspace"   # arbitrary command on the VM host
    uv run python microvm.py agent "change the button color to green, run the app, screenshot it"
    uv run python microvm.py logs / suspend / resume / terminate / clean

State persists in .state.json. The Claude token is read from .claude-token (or
$CLAUDE_CODE_OAUTH_TOKEN) at runtime and injected into the VM — never baked.
"""

import argparse
import base64
import json
import os
import pathlib
import shlex
import sys
import time
import urllib.error
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).parent
IMAGE_DIR = HERE / "image"
WORKSPACE_DIR = HERE / "workspace"
STATE_FILE = HERE / ".state.json"
TOKEN_FILE = HERE / ".claude-token"

REGION = os.environ.get("REGION", "us-east-1")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "ryu-agent-sandbox")
ROLE_NAME = os.environ.get("ROLE_NAME", "MicrovmBuildRole")
ZIP_KEY = "agent-sandbox.zip"
MEMORY_MIB = int(os.environ.get("MEMORY_MIB", "4096"))

BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
IDLE_POLICY = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 1800, "suspendedDurationSeconds": 3600}

DEFAULT_TASK = (
    "Change the frontend button color from blue to green in /workspace/frontend/index.html. "
    "Then run the app (see CLAUDE.md), and use Playwright to screenshot the frontend at "
    "http://localhost:3000 and save it to /workspace/screenshots/button.png."
)


def state_load() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def state_save(**kw) -> dict:
    s = state_load()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    return s


def need(key: str):
    val = state_load().get(key)
    if not val:
        sys.exit(f"missing '{key}' in state — run the earlier steps first")
    return val


def mv():
    return boto3.client("lambda-microvms", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return os.environ.get("BUCKET", f"ryu-microvm-poc-{account_id()}")


def image_arn() -> str:
    return (
        state_load().get("image_arn")
        or f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{IMAGE_NAME}"
    )


# ---- a thin client over the in-VM debug-exec server (:9000) ----------------
def vm_exec(cmd: str, timeout: int = 120) -> dict:
    """POST a shell command to the VM's debug-exec; return {exit,stdout,stderr}."""
    endpoint, token = need("endpoint"), need("token")
    req = urllib.request.Request(
        f"https://{endpoint}/",
        data=cmd.encode(),
        headers={"X-aws-proxy-auth": token, "X-aws-proxy-port": "9000"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _print_exec(out: dict):
    print(out.get("stdout", ""), end="")
    if out.get("stderr"):
        print(out["stderr"], end="")
    if out.get("exit"):
        print(f"[exit {out['exit']}]")


# ---- lifecycle (mirrors POC 01/02) -----------------------------------------
def cmd_check(_):
    try:
        ident = boto3.client("sts").get_caller_identity()
        print(f"  creds OK   acct={ident['Account']}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"  no creds: {e}")
    mv()
    print(f"  client OK  region={REGION} image={IMAGE_NAME} mem={MEMORY_MIB}MiB")
    print(f"  token: {'.claude-token present' if TOKEN_FILE.exists() else 'MISSING (needed only for `agent`)'}")


TRUST = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": ["sts:AssumeRole", "sts:TagSession"]}],
}


def perm_policy(bucket: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": "arn:aws:logs:*:*:*"}]}


def cmd_prereqs(_):
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam")
    bucket = bucket_name()
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        kw = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=bucket, **kw)
    try:
        iam.get_role(RoleName=ROLE_NAME)
    except ClientError:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST))
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="microvm-build", PolicyDocument=json.dumps(perm_policy(bucket)))
    state_save(bucket=bucket, role_arn=f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}")
    print(f"==> bucket {bucket} + role {ROLE_NAME} ready; sleeping 10s for IAM")
    time.sleep(10)


def cmd_package(_):
    """Zip the build context: image/* at the zip root (Dockerfile included) +
    the workspace/ tree under workspace/."""
    bucket = state_load().get("bucket") or bucket_name()
    zip_path = HERE / ZIP_KEY
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(IMAGE_DIR.iterdir()):
            if f.is_file():
                z.write(f, f.name)  # Dockerfile, debug-exec.py, entrypoint.sh, app-up.sh, shot.cjs
        for p in sorted(WORKSPACE_DIR.rglob("*")):
            if p.is_file() and "node_modules" not in p.parts and p.name != ".claude-token":
                z.write(p, f"workspace/{p.relative_to(WORKSPACE_DIR)}")
    boto3.client("s3", region_name=REGION).upload_file(str(zip_path), bucket, ZIP_KEY)
    print(f"==> uploaded s3://{bucket}/{ZIP_KEY}")


def cmd_build(_):
    s = state_load()
    bucket = s.get("bucket") or bucket_name()
    common = dict(codeArtifact={"uri": f"s3://{bucket}/{ZIP_KEY}"}, baseImageArn=BASE_IMAGE_ARN,
                  buildRoleArn=s["role_arn"], resources=[{"minimumMemoryInMiB": MEMORY_MIB}])
    try:
        resp = mv().create_microvm_image(name=IMAGE_NAME, **common)
        print("==> create-microvm-image (new image)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("ConflictException", "ResourceConflictException") or "exist" in str(e).lower():
            resp = mv().update_microvm_image(imageIdentifier=image_arn(), **common)
            print("==> update-microvm-image (new version)")
        else:
            raise
    if resp.get("imageArn"):
        state_save(image_arn=resp["imageArn"])
    print(f"    {resp.get('imageArn', IMAGE_NAME)}\n    poll: uv run python microvm.py wait-image")


def cmd_wait_image(_):
    while True:
        img = mv().get_microvm_image(imageIdentifier=image_arn())
        st = img.get("state")
        print(f"    image state: {st}")
        if st in ("CREATED", "UPDATED"):
            state_save(image_arn=img.get("imageArn", image_arn()))
            print("==> image ready")
            return
        if st and "FAILED" in st:
            sys.exit(f"build failed — check CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        time.sleep(10)


def cmd_run(_):
    resp = mv().run_microvm(imageIdentifier=image_arn(), ingressNetworkConnectors=[INGRESS],
                            egressNetworkConnectors=[EGRESS], idlePolicy=IDLE_POLICY)
    state_save(microvm_id=resp["microvmId"], endpoint=resp["endpoint"])
    print(f"==> microvm {resp['microvmId']}\n    endpoint https://{resp['endpoint']}")


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
    print("==> token minted (valid 60 min)")


# ---- driving the sandbox ----------------------------------------------------
def cmd_shell(args):
    if args and getattr(args, "rest", None):
        _print_exec(vm_exec(" ".join(args.rest)))
        return
    endpoint = need("endpoint")
    print(f"microvm shell @ {endpoint} (runs on VM host; ^D to exit)")
    while True:
        try:
            cmd = input("vm$ ")
        except EOFError:
            print()
            break
        if cmd.strip():
            _print_exec(vm_exec(cmd))


def cmd_app_up(_):
    """Start redis + backend + frontend inside the VM (no Docker)."""
    _print_exec(vm_exec("bash /opt/app-up.sh", timeout=120))


def cmd_shot(args):
    """Playwright screenshot of the frontend -> /workspace/screenshots/button.png.

    Retries once: the first Chromium launch in a fresh VM is cold/slow."""
    url = args.rest[0] if args and args.rest else "http://localhost:3000"
    out = args.rest[1] if args and len(args.rest) > 1 else "/workspace/screenshots/button.png"
    cmd = f"timeout 90 node /opt/shot.cjs {shlex.quote(url)} {shlex.quote(out)} 2>&1"
    for attempt in (1, 2):
        res = vm_exec(cmd, timeout=110)
        if "saved" in res.get("stdout", ""):
            print(res["stdout"], end="")
            return
        print(f"  (shot attempt {attempt} cold/slow; retrying) {res.get('stdout','').strip()[:120]}")
    print("  shot failed twice — inspect with `shell`")


def cmd_pull(args):
    """Copy a file out of the VM: pull <vm_path> [local_path]."""
    if not args or not args.rest:
        sys.exit("usage: pull <vm_path> [local_path]")
    vm_path = args.rest[0]
    local = pathlib.Path(args.rest[1]) if len(args.rest) > 1 else HERE / pathlib.Path(vm_path).name
    out = vm_exec(f"base64 -w0 {shlex.quote(vm_path)}", timeout=120)
    if out.get("exit"):
        sys.exit(f"pull failed: {out.get('stderr', '').strip()}")
    local.write_bytes(base64.b64decode(out["stdout"]))
    print(f"==> {vm_path} -> {local} ({local.stat().st_size} bytes)")


def _claude_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    sys.exit("no Claude token — put it in .claude-token or export CLAUDE_CODE_OAUTH_TOKEN")


def cmd_agent(args):
    """Run Claude Code inside the VM on a task (background; poll with `agent-log`)."""
    tok = _claude_token()
    task = " ".join(args.rest) if args and args.rest else DEFAULT_TASK
    cmd = (
        f"cd /workspace && CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(tok)} "
        f"nohup claude -p {shlex.quote(task)} --dangerously-skip-permissions "
        f">/workspace/claude.log 2>&1 & echo started pid $!"
    )
    _print_exec(vm_exec(cmd, timeout=60))
    print("==> claude running; tail with: uv run python microvm.py agent-log")


def cmd_agent_log(_):
    _print_exec(vm_exec("tail -n 60 /workspace/claude.log 2>&1 || echo '(no log yet)'"))


def cmd_logs(_):
    logs = boto3.client("logs", region_name=REGION)
    group = f"/aws/lambda-microvms/{IMAGE_NAME}"
    streams = logs.describe_log_streams(logGroupName=group, orderBy="LastEventTime", descending=True, limit=1).get("logStreams", [])
    if not streams:
        sys.exit(f"no log streams in {group} yet")
    stream = streams[0]["logStreamName"]
    print(f"==> {group} :: {stream}\n")
    for e in logs.get_log_events(logGroupName=group, logStreamName=stream, limit=200, startFromHead=False)["events"]:
        print(e["message"].rstrip())


def _simple(method: str, label: str):
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
            print("    terminated microvm")
        except ClientError as e:
            print(f"    (terminate skipped: {e})")
    try:
        mv().delete_microvm_image(imageIdentifier=image_arn())
        print("    deleted image")
    except ClientError as e:
        print(f"    (image delete skipped: {e})")
    STATE_FILE.unlink(missing_ok=True)
    print("==> cleaned (bucket/role kept)")


COMMANDS = {
    "check": cmd_check, "prereqs": cmd_prereqs, "package": cmd_package, "build": cmd_build,
    "wait-image": cmd_wait_image, "run": cmd_run, "wait": cmd_wait, "token": cmd_token,
    "shell": cmd_shell, "app-up": cmd_app_up, "shot": cmd_shot, "pull": cmd_pull,
    "agent": cmd_agent, "agent-log": cmd_agent_log, "logs": cmd_logs,
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
