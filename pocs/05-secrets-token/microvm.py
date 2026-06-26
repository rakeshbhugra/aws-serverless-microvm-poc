#!/usr/bin/env python3
"""Drive the secrets-token MicroVM (roadmap track 5 — credential from Secrets Manager).

A light Debian image (Node + Python + Redis + Claude CLI) running an
agent-server on :9100 that runs `claude -p --output-format stream-json`, buffers
each event into a Redis Stream, and relays it over SSE.

    uv run python microvm.py check / prereqs / package / build / wait-image
    uv run python microvm.py run / wait / token
    uv run python microvm.py demo-stream          # stream a canned producer (NO secret) — proves transport
    uv run python microvm.py agent-stream "task"  # run Claude, stream its events live
    uv run python microvm.py shell "..." / logs / suspend / resume / terminate / clean

Unlike POC 4, the Claude credential is NOT read locally or sent in the request.
The VM assumes a runtime role (executionRoleArn) and fetches it from Secrets
Manager itself — the operator never handles the credential.
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).parent
IMAGE_DIR = HERE / "image"
WORKSPACE_DIR = HERE / "workspace"
STATE_FILE = HERE / ".state.json"

REGION = os.environ.get("REGION", "us-east-1")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "ryu-secrets-token")
ROLE_NAME = os.environ.get("ROLE_NAME", "MicrovmBuildRole")
RUNTIME_ROLE_NAME = os.environ.get("RUNTIME_ROLE_NAME", "QhiveMicrovmRuntimeRole")
SECRET_NAME = os.environ.get("SECRET_NAME", "microvm/claude-api-key")
ZIP_KEY = "secrets-token.zip"
MEMORY_MIB = int(os.environ.get("MEMORY_MIB", "2048"))

BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
IDLE_POLICY = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 1800, "suspendedDurationSeconds": 3600}

DEFAULT_TASK = "Add a `farewell(name)` function to hello.py that returns a goodbye string, then run it."


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


# ---- lifecycle (same shape as POC 01-03) -----------------------------------
def cmd_check(_):
    boto3.client("sts").get_caller_identity()
    mv()
    sm = "ok"
    try:
        boto3.client("secretsmanager", region_name=REGION).describe_secret(SecretId=SECRET_NAME)
    except ClientError as e:
        sm = f"MISSING ({e.response.get('Error', {}).get('Code')})"
    print(f"  OK region={REGION} image={IMAGE_NAME} mem={MEMORY_MIB}MiB "
          f"runtime_role={RUNTIME_ROLE_NAME} secret={SECRET_NAME} [{sm}]")


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
    # Runtime role is pre-provisioned by the operator (grants the VM secretsmanager:GetSecretValue
    # on the Claude credential). We only resolve its ARN here — never create/modify it.
    try:
        runtime_arn = iam.get_role(RoleName=RUNTIME_ROLE_NAME)["Role"]["Arn"]
    except ClientError:
        sys.exit(f"runtime role '{RUNTIME_ROLE_NAME}' not found — create it (trust lambda.amazonaws.com, "
                 f"grant secretsmanager:GetSecretValue on {SECRET_NAME}) or set RUNTIME_ROLE_NAME")
    state_save(bucket=b, role_arn=f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}", runtime_role_arn=runtime_arn)
    print(f"==> bucket + build role ready; runtime role {runtime_arn}; sleeping 10s for IAM")
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
    common = dict(codeArtifact={"uri": f"s3://{b}/{ZIP_KEY}"}, baseImageArn=BASE_IMAGE_ARN,
                  buildRoleArn=s["role_arn"], resources=[{"minimumMemoryInMiB": MEMORY_MIB}])
    try:
        resp = mv().create_microvm_image(name=IMAGE_NAME, **common)
        print("==> create-microvm-image")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("ConflictException", "ResourceConflictException") or "exist" in str(e).lower():
            resp = mv().update_microvm_image(imageIdentifier=image_arn(), **common)
            print("==> update-microvm-image")
        else:
            raise
    if resp.get("imageArn"):
        state_save(image_arn=resp["imageArn"])
    state_save(image_version=resp.get("imageVersion"))  # track THIS build's version
    print(f"    building version {resp.get('imageVersion')}; poll: uv run python microvm.py wait-image")


def cmd_wait_image(_):
    """Poll the specific version we just built (not image-level state, which can
    show a stale UPDATED from a prior successful version while a new one fails)."""
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
    # executionRoleArn gives the running VM AWS credentials so it can fetch the
    # Claude credential from Secrets Manager itself — the POC-5 change.
    resp = mv().run_microvm(imageIdentifier=image_arn(), ingressNetworkConnectors=[INGRESS],
                            egressNetworkConnectors=[EGRESS], idlePolicy=IDLE_POLICY,
                            executionRoleArn=need("runtime_role_arn"))
    state_save(microvm_id=resp["microvmId"], endpoint=resp["endpoint"])
    print(f"==> microvm {resp['microvmId']}\n    https://{resp['endpoint']}")


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


# ---- the streaming bits -----------------------------------------------------
def _post_run(body: dict) -> str:
    endpoint = need("endpoint")
    req = urllib.request.Request(f"https://{endpoint}/run", data=json.dumps(body).encode(),
                                 headers={**_hdr("9100"), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    print(f"  run #{resp['run']} ({resp['stream']})")
    return str(resp["run"])


def _consume_stream(run: str, from_id: str = "0"):
    """Open the SSE stream for one run and print events live until 'done'."""
    endpoint = need("endpoint")
    req = urllib.request.Request(f"https://{endpoint}/stream?run={run}&from={from_id}", headers=_hdr("9100"))
    print(f"  --- streaming run #{run} ---")
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode(errors="replace").rstrip("\n")
            if line.startswith("data:"):
                data = line[5:].strip()
                try:
                    obj = json.loads(data)
                except ValueError:
                    print("   ", data)
                    continue
                t = obj.get("type")
                if t == "start":
                    print(f"    ▶ start: {obj.get('task')}")
                elif t == "claude":
                    print(f"    · {obj.get('line')}")
                elif t == "done":
                    print(f"    ■ done (exit {obj.get('exit')})")
                    return


def cmd_demo_stream(_):
    """Prove the transport with a canned producer — no Claude token needed."""
    run = _post_run({"demo": True})
    _consume_stream(run)


def cmd_agent_stream(args):
    """Run Claude on a task inside the VM and stream its events live.

    No credential is handled here — the VM fetches it from Secrets Manager via
    its runtime role. We only send the task.
    """
    task = " ".join(args.rest) if args and args.rest else DEFAULT_TASK
    run = _post_run({"task": task})
    _consume_stream(run)


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
            print("    terminated")
        except ClientError as e:
            print(f"    (terminate skipped: {e})")
    try:
        mv().delete_microvm_image(imageIdentifier=image_arn())
        print("    image deleted")
    except ClientError as e:
        print(f"    (image delete skipped: {e})")
    STATE_FILE.unlink(missing_ok=True)
    print("==> cleaned")


COMMANDS = {
    "check": cmd_check, "prereqs": cmd_prereqs, "package": cmd_package, "build": cmd_build,
    "wait-image": cmd_wait_image, "run": cmd_run, "wait": cmd_wait, "token": cmd_token,
    "demo-stream": cmd_demo_stream, "agent-stream": cmd_agent_stream, "shell": cmd_shell, "logs": cmd_logs,
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
