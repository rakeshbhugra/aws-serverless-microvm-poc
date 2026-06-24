#!/usr/bin/env python3
"""Drive the MicroVM lifecycle for the docker-compose POC (roadmap track 2).

Same flow as POC 01, with two differences that make `docker compose up` work
INSIDE the VM:

  * package() zips the whole stack/ dir (Dockerfile at zip root + the compose
    project) rather than two files.
  * build() passes additionalOsCapabilities=["ALL"] so the in-VM Docker daemon
    has the privileges it needs, and a larger memory baseline for dockerd+build.

    uv run python microvm.py check        # creds + client present?
    uv run python microvm.py prereqs       # S3 bucket + IAM build role
    uv run python microvm.py package       # zip stack/ -> S3
    uv run python microvm.py build         # create-microvm-image (ALL caps)
    uv run python microvm.py wait-image    # poll until CREATED (slow)
    uv run python microvm.py run           # run-microvm
    uv run python microvm.py wait          # poll until RUNNING
    uv run python microvm.py token         # 30-min auth token
    uv run python microvm.py curl          # -> {"count":1,...}; again -> count:2
    uv run python microvm.py suspend / resume / terminate / clean

State persists in .state.json between calls.
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).parent
STACK_DIR = HERE / "stack"
STATE_FILE = HERE / ".state.json"

REGION = os.environ.get("REGION", "us-east-1")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "ryu-compose-stack")
ROLE_NAME = os.environ.get("ROLE_NAME", "MicrovmBuildRole")
ZIP_KEY = "compose-stack.zip"
MEMORY_MIB = int(os.environ.get("MEMORY_MIB", "4096"))  # dockerd + build want headroom

BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"

IDLE_POLICY = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 1800}


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
    """boto3 client for the Lambda MicroVMs API (needs botocore >= 1.43.36)."""
    return boto3.client("lambda-microvms", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return os.environ.get("BUCKET", f"ryu-microvm-poc-{account_id()}")


def image_arn() -> str:
    """Full image ARN — get/run/delete need the ARN, not the bare name."""
    return (
        state_load().get("image_arn")
        or f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{IMAGE_NAME}"
    )


def cmd_check(_):
    try:
        ident = boto3.client("sts").get_caller_identity()
        print(f"  creds OK   acct={ident['Account']} arn={ident['Arn']}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"  no creds: {e}\n  -> configure AWS access on this box first")
    try:
        mv()
        print("  boto3 knows 'lambda-microvms' client")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"  no lambda-microvms client: {e}\n  -> uv add boto3 (botocore >= 1.43.36)")
    print(f"  region={REGION}  image={IMAGE_NAME}  bucket={bucket_name()}  mem={MEMORY_MIB}MiB")


TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
        }
    ],
}


def perm_policy(bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/*"},
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "arn:aws:logs:*:*:*",
            },
        ],
    }


def cmd_prereqs(_):
    """Create the S3 bucket + IAM build role (idempotent). Same as POC 01 —
    the bucket/role are shared across POCs, so this is usually a no-op here."""
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam")
    bucket = bucket_name()

    print(f"==> S3 bucket: {bucket}")
    try:
        s3.head_bucket(Bucket=bucket)
        print("    exists")
    except ClientError:
        kw = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=bucket, **kw)
        print("    created")

    print(f"==> IAM build role: {ROLE_NAME}")
    try:
        iam.get_role(RoleName=ROLE_NAME)
        print("    exists")
    except ClientError:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST))
        print("    created")
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="microvm-build", PolicyDocument=json.dumps(perm_policy(bucket)))
    state_save(bucket=bucket, role_arn=f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}")
    print("    policy attached; sleeping 10s for IAM propagation")
    time.sleep(10)


def cmd_package(_):
    """Zip the entire stack/ dir (Dockerfile lands at the zip root) -> S3.

    Walks stack/ recursively so the MicroVM image Dockerfile, docker-compose.yml,
    entrypoint.sh, and backend/ all travel together. Lambda runs the Dockerfile
    from the zip root, which COPYs the rest in.
    """
    bucket = state_load().get("bucket") or bucket_name()
    zip_path = HERE / ZIP_KEY
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(STACK_DIR.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(STACK_DIR))
    boto3.client("s3", region_name=REGION).upload_file(str(zip_path), bucket, ZIP_KEY)
    print(f"==> uploaded s3://{bucket}/{ZIP_KEY}")


def cmd_build(_):
    """Build the image — create if new, else update (ship a new version).

    create-microvm-image with ALL OS capabilities (so Docker can run in-VM) and a
    4 GB baseline (dockerd + build need headroom). If the image already exists,
    fall back to update-microvm-image so `build` is re-runnable after edits.
    """
    s = state_load()
    bucket = s.get("bucket") or bucket_name()
    common = dict(
        codeArtifact={"uri": f"s3://{bucket}/{ZIP_KEY}"},
        baseImageArn=BASE_IMAGE_ARN,
        buildRoleArn=s["role_arn"],
        additionalOsCapabilities=["ALL"],
        resources=[{"minimumMemoryInMiB": MEMORY_MIB}],
    )
    try:
        resp = mv().create_microvm_image(name=IMAGE_NAME, **common)
        print("==> create-microvm-image (new image)")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ConflictException", "ResourceConflictException") or "exist" in str(e).lower():
            resp = mv().update_microvm_image(imageIdentifier=image_arn(), **common)
            print("==> update-microvm-image (new version of existing image)")
        else:
            raise
    arn = resp.get("imageArn")
    if arn:
        state_save(image_arn=arn)
    print(f"    {arn or IMAGE_NAME}\n    poll with: uv run python microvm.py wait-image")


def cmd_wait_image(_):
    """Poll until the build finishes (CREATED/UPDATED) or fails."""
    while True:
        img = mv().get_microvm_image(imageIdentifier=image_arn())
        st = img.get("state")
        print(f"    image state: {st}")
        if st in ("CREATED", "UPDATED"):
            state_save(image_arn=img.get("imageArn", image_arn()))
            print(f"==> image ready: {img.get('imageArn', IMAGE_NAME)}")
            return
        if st and "FAILED" in st:
            sys.exit(f"build failed — check CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        time.sleep(10)


def cmd_run(_):
    resp = mv().run_microvm(
        imageIdentifier=image_arn(),
        ingressNetworkConnectors=[INGRESS],
        egressNetworkConnectors=[EGRESS],
        idlePolicy=IDLE_POLICY,
    )
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
    resp = mv().create_microvm_auth_token(
        microvmIdentifier=mid, expirationInMinutes=30, allowedPorts=[{"allPorts": {}}]
    )
    state_save(token=resp["authToken"]["X-aws-proxy-auth"])
    print("==> token minted (valid 30 min)")


def cmd_curl(_):
    endpoint, token = need("endpoint"), need("token")
    req = urllib.request.Request(
        f"https://{endpoint}/", headers={"X-aws-proxy-auth": token, "X-aws-proxy-port": "8080"}
    )
    print(f"==> GET https://{endpoint}/")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {e.reason}\n{e.read().decode(errors='replace')[:500]}")
        print("  502 = app not responding (try `logs` / `shell`); 403 = bad/expired token or port")


def cmd_shell(args):
    """Run commands inside the VM via the baked debug-exec server (:9000).

    `shell "docker ps"` runs one command; bare `shell` opens an interactive loop.
    Commands run on the VM host — where Docker lives — so `docker ps`,
    `docker logs <c>`, `docker exec backend cat /etc/resolv.conf` all work. ^D to
    exit. Auth is the MicroVM JWE token + VM boundary (ingress gates :9000).
    """
    endpoint, token = need("endpoint"), need("token")

    def run(cmd: str):
        req = urllib.request.Request(
            f"https://{endpoint}/",
            data=cmd.encode(),
            headers={"X-aws-proxy-auth": token, "X-aws-proxy-port": "9000"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} {e.reason} — VM running? token fresh? debug-exec up?")
            return
        print(out.get("stdout", ""), end="")
        print(out.get("stderr", ""), end="")
        if out.get("exit"):
            print(f"[exit {out['exit']}]")

    if args and getattr(args, "rest", None):
        run(" ".join(args.rest))
        return
    print(f"microvm shell @ {endpoint}  (runs on VM host; ^D to exit)")
    while True:
        try:
            cmd = input("vm$ ")
        except EOFError:
            print()
            break
        if cmd.strip():
            run(cmd)


def cmd_logs(_):
    """Tail the MicroVM's CloudWatch logs — its in-VM stdout/stderr.

    The VM streams everything the entrypoint/app prints to
    /aws/lambda-microvms/<image>. This grabs the most recent stream's last 200
    lines — the fastest way to see why a VM 502'd or terminated, no shell needed.
    """
    logs = boto3.client("logs", region_name=REGION)
    group = f"/aws/lambda-microvms/{IMAGE_NAME}"
    streams = logs.describe_log_streams(
        logGroupName=group, orderBy="LastEventTime", descending=True, limit=1
    ).get("logStreams", [])
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
    print("==> cleaned (IAM role + S3 bucket left in place — shared across POCs)")


def cmd_all(args):
    for fn in (cmd_prereqs, cmd_package, cmd_build, cmd_wait_image, cmd_run, cmd_wait, cmd_token, cmd_curl):
        fn(args)


COMMANDS = {
    "check": cmd_check,
    "prereqs": cmd_prereqs,
    "package": cmd_package,
    "build": cmd_build,
    "wait-image": cmd_wait_image,
    "run": cmd_run,
    "wait": cmd_wait,
    "token": cmd_token,
    "curl": cmd_curl,
    "shell": cmd_shell,
    "logs": cmd_logs,
    "suspend": _simple("suspend_microvm", "suspended"),
    "resume": _simple("resume_microvm", "resumed"),
    "terminate": _simple("terminate_microvm", "terminated"),
    "clean": cmd_clean,
    "all": cmd_all,
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    p.add_argument("rest", nargs="*", help="extra args (e.g. a command for `shell`)")
    args = p.parse_args()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
